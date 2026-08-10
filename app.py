# -*- coding: utf-8 -*-
# =====================================================================
#  CONTADOR DE REVISÕES DESTE app.py: 60
#  (some +1 sempre que uma versão nova for gerada)
# =====================================================================
"""
MOTOR DE ORÇAMENTOS — Frota Macedo  (100% online: FastAPI no Render)

O operador abre a página, sobe as NOTAS/DAVs (PDF ou imagem) e clica em
"Gerar e enviar ao Dropbox". O motor faz TUDO no Dropbox online (não devolve ZIP):
  - gera 1 orçamento (PDF) por ticket em ORCAMENTOS MONTADOS/<mês>/<loja>/
    e uma cópia em "1 - ORÇAMENTOS NÃO LANÇADOS" (p/ o robô do Trílogo);
  - roteia cada nota (página a página) em NOTAS INCLUIDAS ORCAMENTO/<aba>,
    SEM TICKET ou TICKET NAO ASSOCIADO;
  - mantém a planilha de controle do mês atualizada (e oferece p/ baixar no app);
  - atualiza as planilhas de correção (SEM TICKET / TICKET NAO ASSOCIADO).

Leitura das notas (visão): Groq (Llama/Qwen) se GROQ_API_KEY existir; senão Gemini.
Ticket -> loja/aba: tabela `chamados` no Supabase (alimentada pelo robô do Trílogo).
Regras (dos scripts): +20% no unitário (sem mencionar), rateio de entrega,
1 orçamento por ticket, dedup pelo NÚMERO DA NOTA (permite +1 orçamento por ticket).

Interface: FastAPI + uvicorn (sem Gradio). Login por HTTP Basic (APP_USER/APP_PASS).

Segredos no Render (Environment):
  Leitura:  GROQ_API_KEY   (recomendado)  ou  GEMINI_API_KEY
  Supabase: SUPABASE_URL, SUPABASE_SERVICE_KEY
  Login:    APP_USER, APP_PASS
  Dropbox:  DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN, DROPBOX_BASE
  Opcionais: GROQ_MODEL, GEMINI_MODEL, FAT_NOME, FAT_CNPJ
"""
import os, re, json, subprocess, tempfile, datetime, unicodedata, urllib.parse, urllib.request, urllib.error
import google.generativeai as genai
import dropbox_rateio

BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(BASE, "scripts")
ASSETS = os.path.join(BASE, "assets")
LOGO = os.path.join(ASSETS, "logo_frota.jpg")
CAD = json.load(open(os.path.join(ASSETS, "cadastro_lojas.json"), encoding="utf-8"))

GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")   # ajustável; o motor ainda tem fallback abaixo
_GEMINI_OK = None   # modelo que funcionou (cache entre chamadas)
GROQ_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.6-27b")
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
# anon key (pública) — usada para validar o token do usuário do FrotaHub
SB_ANON = os.environ.get("SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZhYWxnZmJ1Z3Zla2J1aGh0YXR0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU4NzA0OTIsImV4cCI6MjEwMTQ0NjQ5Mn0.Vwba3hsm43bwOXMXf2iQMkCWXqrGZaHKojHvV6mhFSI")
# base do PCO no Dropbox (novo layout FROTAHUB)
PCO_BASE = os.environ.get("PCO_BASE", "/FROTAHUB/1 - ADMINISTRATIVO/1 - PCO")
# --- ENVIAR_PCO: SMTP + destinatários (padrão = TESTE p/ igor@; trocar por env p/ ir ao ar) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.frotamacedo.com.br")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
# Render bloqueia portas de SMTP -> preferimos a API HTTP do Brevo quando houver chave
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
PCO_FROM  = os.environ.get("PCO_FROM", SMTP_USER or "pco@frotamacedo.com.br")
def _lista_env(k, padrao):
    return [x.strip() for x in os.environ.get(k, padrao).split(",") if x.strip()]
PCO_TO      = _lista_env("PCO_TO", "igor@frotamacedo.com.br")
PCO_CC      = _lista_env("PCO_CC", "")
PCO_BLOQ_TO = _lista_env("PCO_BLOQ_TO", "igor@frotamacedo.com.br")
ASSINATURA = ("<b>Isabele Melissa</b><br><b>Administrativo</b><br>"
              "<b>(85) 98795-5735</b><br><b>Frota Macedo Engenharia LTDA</b>")
FAT_NOME = os.environ.get("FAT_NOME", "FROTA MACEDO ENGENHARIA LTDA")
FAT_CNPJ = os.environ.get("FAT_CNPJ", "27.363.223/0001-70")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

MESES = ["JANEIRO","FEVEREIRO","MARÇO","ABRIL","MAIO","JUNHO","JULHO","AGOSTO",
         "SETEMBRO","OUTUBRO","NOVEMBRO","DEZEMBRO"]

def slug(s):
    return re.sub(r"\s+", "_", (s or "").strip())

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii","ignore").decode().upper()
    return re.sub(r"[^A-Z0-9 ]", " ", s).strip()

def fmt_cnpj(s):
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return str(s).strip() if s else "—"

def cidade_de(endereco):
    """Extrai 'Cidade - UF' de um endereço do cadastro (ex.: '..., Fortaleza - CE, 60175-395')."""
    m = re.search(r",\s*([^,]+?\s*-\s*[A-Z]{2})\b", str(endereco or ""))
    return m.group(1).strip() if m else ""

RE_TICKET = re.compile(r"(?:ticket|ticker|tickte|tikett|tiket|tcket|tck|tk|chamado|cham|\bos\b|or[çc]amento|#)\s*[:/#\.\-]?\s*(\d{5,6})\b", re.I)
def ticket_do_texto(t):
    """Extrai o ticket (5-6 dígitos) de um texto de observação, tolerando rótulos errados."""
    t = str(t or "")
    m = RE_TICKET.search(t)
    if m: return m.group(1)
    m = re.search(r"(?<!\d)(\d{5,6})(?!\d)", t)   # fallback: número isolado de 5-6 dígitos
    return m.group(1) if m else ""

# ---------- leitura das notas (render por página) ----------
def paginas_imagens(path, workdir):
    """Retorna lista de (indice, caminho_png). PDF: render por página; imagem: usa direto."""
    ext = path.lower().rsplit(".", 1)[-1]
    out = []
    if ext in ("jpg", "jpeg", "png"):
        return [(1, path)]
    if ext == "pdf":
        try:
            info = subprocess.run(["pdfinfo", path], capture_output=True, text=True).stdout
            m = re.search(r"Pages:\s+(\d+)", info); n = int(m.group(1)) if m else 1
        except Exception:
            n = 1
        for p in range(1, n+1):
            pref = os.path.join(workdir, f"pg_{os.path.basename(path)}_{p}")
            subprocess.run(["pdftoppm", "-r", "170", "-png", "-f", str(p), "-l", str(p), path, pref],
                           capture_output=True)
            # pdftoppm gera pref-<p>.png (com zero à esquerda dependendo do total)
            cand = [f for f in os.listdir(workdir) if f.startswith(os.path.basename(pref)) and f.endswith(".png")]
            if cand: out.append((p, os.path.join(workdir, sorted(cand)[0])))
        return out
    return out

PROMPT = """Você lê UMA nota fiscal / DAV / orçamento de fornecedor (imagem).
O EMITENTE (Razão Social de quem emitiu) é o "fornecedor". Ignore o destinatário.
Extraia SOMENTE o que estiver na nota e responda em JSON puro (sem texto fora do JSON):
{
 "ticket": "<número do ticket/chamado, 5-6 dígitos. Quase sempre aparece no campo Observação / Dados Complementares / Dados adicionais, precedido por um rótulo que PODE estar com erro de digitação ou abreviado: TICKET, TICKER, TICKTE, TIKET, TCKET, TK, CHAMADO, OS, ORÇAMENTO, ou apenas #. Retorne SÓ os dígitos. Exemplos: 'TICKER:126486 AMAURI COCO' -> 126486 ; 'Obs: tk 125411' -> 125411 ; '#126486' -> 126486. Só use null se não houver mesmo nenhum número de 5-6 dígitos com esse sentido>",
 "num_documento": "<Nº do Documento/Nota, só os dígitos e SEM zeros à esquerda (ex.: '0000018747' -> '18747'); se não houver, null>",
 "fornecedor": "<razão social do EMITENTE>",
 "forma_pagamento": "<ex.: Boleto 30 dias / Plano de Pagamento, se aparecer; senão null>",
 "obs": "<TRANSCREVA VERBATIM, exatamente como está na nota, todo o conteúdo dos campos Observação / Dados Complementares / Dados Adicionais / Informações Complementares (é onde costuma estar o ticket). Se não houver, ''>",
 "itens": [
   {"desc":"<descrição do item, LIMPA: remova o código/SKU do início (ex.: '00000000001633 - CIMENTO TODAS AS OBRAS' -> 'CIMENTO TODAS AS OBRAS') e não inclua NCM/CFOP>", "quant":<número>, "unid":"<UN/PC/SV/KG/…>", "unit":<valor UNITÁRIO BRUTO, sem imposto/desconto/acréscimo>}
 ]
}
Regras: NÃO aplique nenhum acréscimo/desconto — use o valor unitário BRUTO. Inclua item de ENTREGA/FRETE se houver (desc contendo 'ENTREGA' ou 'FRETE'). quant e unit são números (ponto decimal)."""

def _parse_json(txt):
    txt = (txt or "").strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {"ticket": None, "itens": []}

def gemini_le(png_path):
    """Tenta uma lista de modelos e usa o 1º que funcionar (memoriza no _GEMINI_OK)."""
    global _GEMINI_OK
    from PIL import Image
    img = Image.open(png_path)
    candidatos = [_GEMINI_OK] if _GEMINI_OK else list(dict.fromkeys([
        GEMINI_MODEL, "gemini-2.5-flash-lite", "gemini-flash-latest",
        "gemini-2.0-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash",
    ]))
    ultimo = None
    for nome in candidatos:
        if not nome: continue
        try:
            model = genai.GenerativeModel(nome)
            r = model.generate_content([PROMPT, img],
                generation_config={"temperature": 0, "response_mime_type": "application/json"})
            _GEMINI_OK = nome
            return _parse_json(r.text)
        except Exception as e:
            ultimo = e
            m = str(e).lower()
            if any(s in m for s in ("not available", "not found", "404", "no longer",
                                    "unsupported", "permission", "does not exist")):
                continue                 # modelo indisponível p/ essa conta -> tenta o próximo
            raise                        # erro real (ex.: cota) -> propaga
    raise ultimo or RuntimeError("nenhum modelo Gemini disponível")

def groq_le(png_path):
    import base64, io
    from PIL import Image
    # converte SEMPRE p/ PNG de verdade (evita descasar bytes JPEG com rótulo image/png)
    im = Image.open(png_path).convert("RGB")
    if im.width > 1600:                      # reduz p/ caber no limite da API
        im = im.resize((1600, int(im.height * 1600 / im.width)))
    buf = io.BytesIO(); im.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = {
        "model": GROQ_MODEL, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
    }
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    try:
        raw = urllib.request.urlopen(req, timeout=90).read().decode()
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", "ignore")
        except Exception: pass
        raise RuntimeError(f"Groq {e.code}: {body[:300]}") from None
    r = json.loads(raw)
    return _parse_json(r["choices"][0]["message"]["content"])

def ler_nota(png_path):
    """Usa o Groq (visão) se a chave estiver setada; senão o Gemini."""
    return groq_le(png_path) if GROQ_KEY else gemini_le(png_path)

# ---------- ticket -> loja (Supabase chamados) ----------
def busca_chamado(ticket):
    if not (SB_URL and SB_KEY and ticket): return None
    q = urllib.parse.urlencode({"numero": f"eq.{ticket}", "select": "loja,aba,descricao", "limit": "1"})
    req = urllib.request.Request(f"{SB_URL}/rest/v1/chamados?{q}",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        return data[0] if data else None
    except Exception:
        return None

def loja_cadastro(loja_str):
    """Acha a loja no cadastro_lojas.json a partir da 'Unidade' do chamado."""
    m = re.search(r"LOJA\s*0*(\d{1,2})", norm(loja_str))
    if m:
        e = CAD.get(m.group(1).zfill(2))
        if e: return e
    alvo = norm(loja_str)
    for e in CAD.values():
        nomes = [norm(e.get("nome"))] + [norm(a) for a in e.get("apelidos", [])]
        if any(nm and nm in alvo for nm in nomes):
            return e
    return None

# ---------- geração de 1 orçamento ----------
def gera_orcamento(ticket, chamado, itens_brutos, workdir, outdir, data_str, forma_pag):
    prep = json.loads(subprocess.run(["python3", os.path.join(SCRIPTS, "preparar_itens.py")],
        input=json.dumps({"itens": itens_brutos}), capture_output=True, text=True).stdout)
    e = loja_cadastro(chamado.get("loja")) or {}
    num = e.get("numero") or "00"
    nome_loja = e.get("nome") or re.sub(r"^LOJA\s*\d+\s*-\s*", "", chamado.get("loja") or "").strip() or "LOJA"
    cliente = {"nome": f"Mercadinhos São Luiz — {nome_loja.title()}",
               "cnpj": fmt_cnpj(e.get("cnpj")), "endereco": e.get("endereco") or "—",
               "cidade": cidade_de(e.get("endereco"))}
    obra = f"{norm(chamado.get('descricao'))}  #{ticket}"
    dados = {"numero_ticket": str(ticket), "revisao": 1, "data": data_str, "obra": obra,
             "faturamento": {"nome": FAT_NOME, "cnpj": FAT_CNPJ,
                             "forma_pagamento": forma_pag or "", "data_faturamento": data_str},
             "cliente": cliente, "itens": prep["itens"],
             "total_geral": prep["total_geral"], "extenso": prep["extenso"]}
    dj = os.path.join(workdir, f"dados_{ticket}.json")
    json.dump(dados, open(dj, "w"), ensure_ascii=False)
    base = f"{num}_{slug(nome_loja)}_{ticket}"
    pdf = os.path.join(workdir, base + ".pdf")
    subprocess.run(["python3", os.path.join(SCRIPTS, "gerar_pdf.py"), dj, pdf, LOGO],
                   capture_output=True)
    return dict(ticket=ticket, num=num, nome_loja=nome_loja, loja=f"{num} - {nome_loja}",
                total=prep["total_geral"], pdf=pdf, base=base, pdf_ok=os.path.exists(pdf))

# ---------- orquestração ----------
def _nota_nome_pdf(info):
    """Nome da nota roteada no Dropbox: 'TICKET <n> - NOTA <doc>.pdf' (sempre .pdf)."""
    num = re.sub(r'[\\/:*?"<>|]', "", str(info.get("num") or "")).strip()
    tk = info.get("ticket") or ""
    if info["cat"] == "SEM TICKET":
        base = f"TICKET SEMTICKET - NOTA {num}" if num else f"NOTA p{info.get('page', 1)}"
    elif tk:
        base = f"TICKET {tk} - NOTA {num}" if num else f"TICKET {tk} - p{info.get('page', 1)}"
    else:
        base = f"NOTA p{info.get('page', 1)}"
    return base + ".pdf"

def _pagina_pdf(src, page, is_img, workdir, tag):
    """Produz um PDF de 1 página a partir de uma página do arquivo (imagem -> PDF; PDF -> só a página)."""
    out = os.path.join(workdir, f"nota_{tag}.pdf")
    try:
        if is_img:
            from PIL import Image
            Image.open(src).convert("RGB").save(out, "PDF")
        else:
            subprocess.run(["pdfseparate", "-f", str(page), "-l", str(page), src, out], capture_output=True)
        return out if os.path.exists(out) else None
    except Exception:
        return None

def processar(arquivos, planilha_controle=None):
    if not (GEMINI_KEY or GROQ_KEY):
        return None, "⚠️ Configure o segredo GROQ_API_KEY (ou GEMINI_API_KEY)."
    if not (SB_URL and SB_KEY):
        return None, "⚠️ Faltam os segredos SUPABASE_URL / SUPABASE_SERVICE_KEY."
    if not dropbox_rateio.ativo():
        return None, "⚠️ Configure os segredos do Dropbox (DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN/BASE)."
    try:
        access = dropbox_rateio.obter_token()
        if not access: raise RuntimeError("token vazio")
    except Exception as e:
        return None, f"⚠️ Falha ao autenticar no Dropbox: {e}"

    work = tempfile.mkdtemp(prefix="orc_")
    hoje = datetime.date.today()
    mes = f"{MESES[hoje.month-1]} {hoje.year}"
    data_str = hoje.strftime("%d/%m/%Y")
    B = dropbox_rateio.BASE
    dbx_mes = f"{B}/{dropbox_rateio.ORCAMENTOS}/{mes}"
    ATUALIZAR = os.path.join(SCRIPTS, "atualizar_planilha_mensal.py")
    PENDENTES = os.path.join(SCRIPTS, "pendentes.py")
    erros_dbx = []

    # 1) baixa a planilha de controle e o registro de notas já feitas neste mês
    ctrl = os.path.join(work, f"ORÇAMENTOS MONTADOS - {mes}.xlsx")
    ctrl_dbx = f"{dbx_mes}/ORÇAMENTOS MONTADOS - {mes}.xlsx"
    try:
        atual = dropbox_rateio.baixar(access, ctrl_dbx)
        if atual: open(ctrl, "wb").write(atual)
    except Exception as e:
        erros_dbx.append(f"baixar controle: {e}")
    reg_dbx = f"{dbx_mes}/.notas_processadas.json"
    notas_feitas = set()
    try:
        rb = dropbox_rateio.baixar(access, reg_dbx)
        if rb: notas_feitas = set(str(x) for x in json.loads(rb.decode("utf-8")))
    except Exception:
        pass

    # 2) lê as notas (por página); dedup pelo NÚMERO DA NOTA; agrupa por ticket
    por_ticket = {}
    sem_ticket, nao_assoc = [], []
    paginas = []          # cada página vira 1 arquivo roteado no Dropbox
    novas_notas = set()   # notas que VIRARAM orçamento (vão pro registro do mês)
    vistos = set()        # guarda contra repetir a MESMA nota dentro deste lote
    duplicadas = 0
    diag = []             # diagnóstico do que o Gemini leu (p/ notas SEM TICKET)
    for f in (arquivos or []):
        path = f.name if hasattr(f, "name") else f
        is_img = path.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png")
        pgs = paginas_imagens(path, work)
        if not pgs:
            paginas.append({"src": path, "page": 1, "is_img": is_img, "cat": "RESIDUAL", "ticket": "", "num": ""})
            continue
        for idx, png in pgs:
            try:
                nota = ler_nota(png)
            except Exception as e:
                nota = {"ticket": None, "itens": [], "fornecedor": None, "_erro": str(e)}
            try:
                print("LEITURA:", json.dumps({k: nota.get(k) for k in ("ticket","num_documento","fornecedor","obs","_erro")}, ensure_ascii=False)[:500], flush=True)
            except Exception:
                pass
            ticket = re.sub(r"\D", "", str(nota.get("ticket") or ""))
            if not ticket:                                   # rede de segurança: extrai do texto da observação
                ticket = ticket_do_texto(nota.get("obs"))
            itens = nota.get("itens") or []
            numdoc = str(nota.get("num_documento") or "").strip()
            # dedup por número da nota: nota já ORÇADA no mês, ou repetida neste mesmo lote -> ignora
            if numdoc and (numdoc in notas_feitas or numdoc in vistos):
                duplicadas += 1; continue
            if numdoc: vistos.add(numdoc)
            reg = {"nota": numdoc, "fornecedor": nota.get("fornecedor") or "", "ticket": ticket, "loja": ""}
            pg = {"src": path, "page": idx, "is_img": is_img, "ticket": ticket, "num": numdoc}
            if not ticket:
                diag.append({"forn": nota.get("fornecedor") or "?", "obs": str(nota.get("obs") or "")[:140],
                             "nitens": len(itens), "err": nota.get("_erro")})
                sem_ticket.append({**reg, "status": "SEM TICKET"}); pg["cat"] = "SEM TICKET"; paginas.append(pg); continue
            ch = busca_chamado(ticket)
            if not ch:
                nao_assoc.append({**reg, "status": "TICKET NÃO ASSOCIADO"}); pg["cat"] = "NAO ASSOCIADO"; paginas.append(pg); continue
            pg["cat"] = (ch.get("aba") or "CIVIL").upper(); paginas.append(pg)
            g = por_ticket.setdefault(ticket, {"chamado": ch, "itens": [], "forma": nota.get("forma_pagamento"), "notas": set()})
            g["itens"].extend(itens)
            if numdoc: g["notas"].add(numdoc)
            if nota.get("forma_pagamento") and not g["forma"]: g["forma"] = nota.get("forma_pagamento")

    # 3) gera 1 orçamento por ticket -> sobe PDF (mês/loja + não lançados) + acrescenta no controle
    feitos = []
    for ticket, g in por_ticket.items():
        if not g["itens"]: continue
        try:
            d = gera_orcamento(ticket, g["chamado"], g["itens"], work, work, data_str, g["forma"])
        except Exception as e:
            nao_assoc.append({"nota": "", "fornecedor": "", "status": f"ERRO: {e}", "ticket": ticket, "loja": ""}); continue
        feitos.append(d)
        novas_notas |= g.get("notas", set())      # só REGISTRA as notas que viraram orçamento
        if d["pdf_ok"]:
            subprocess.run(["python3", ATUALIZAR, "--xlsx", ctrl, "--ticket", str(ticket),
                            "--loja", d["nome_loja"], "--pdf", d["pdf"], "--data", data_str, "--append"], capture_output=True)
            nome_pdf = d["base"] + ".pdf"
            try: dropbox_rateio.subir(access, d["pdf"], f"{dbx_mes}/{d['num']}_{slug(d['nome_loja'])}/{nome_pdf}")
            except Exception as e: erros_dbx.append(f"PDF {ticket}: {e}")
            try: dropbox_rateio.subir(access, d["pdf"], f"{B}/{dropbox_rateio.NAO_LANCADOS}/{nome_pdf}")
            except Exception as e: erros_dbx.append(f"não lançados {ticket}: {e}")

    # 4) sobe controle atualizado + registro de notas processadas
    ctrl_ok = os.path.exists(ctrl)
    if ctrl_ok:
        try: dropbox_rateio.subir(access, ctrl, ctrl_dbx, overwrite=True)
        except Exception as e: erros_dbx.append(f"subir controle: {e}")
    if novas_notas:
        try:
            dropbox_rateio.subir_bytes(access, json.dumps(sorted(notas_feitas | novas_notas)).encode("utf-8"),
                                       reg_dbx, overwrite=True)
        except Exception as e:
            erros_dbx.append(f"registro notas: {e}")

    # 5) planilhas de correção por categoria (baixa -> adiciona -> sobe)
    def atualiza_correcao(lista, subpasta, arq):
        if not lista: return
        dbxp = f"{B}/{subpasta}/{arq}"
        local = os.path.join(work, arq)
        try:
            atual = dropbox_rateio.baixar(access, dbxp)
            if atual: open(local, "wb").write(atual)
        except Exception as e:
            erros_dbx.append(f"baixar {arq}: {e}")
        inp = os.path.join(work, "p_" + re.sub(r"\W+", "_", subpasta) + ".json")
        json.dump(lista, open(inp, "w"), ensure_ascii=False)
        subprocess.run(["python3", PENDENTES, "add", "--xlsx", local, "--input", inp], capture_output=True)
        if os.path.exists(local):
            try: dropbox_rateio.subir(access, local, dbxp, overwrite=True)
            except Exception as e: erros_dbx.append(f"subir {arq}: {e}")
    atualiza_correcao(sem_ticket, "SEM TICKET", "NOTAS - SEM TICKET.xlsx")
    atualiza_correcao(nao_assoc, "TICKET NAO ASSOCIADO", "NOTAS - TICKET NAO ASSOCIADO.xlsx")

    # 6) roteia as notas página a página (multipágina: cada página vai pro seu destino)
    itens_dbx = []
    for i, pg in enumerate(paginas):
        local = _pagina_pdf(pg["src"], pg["page"], pg["is_img"], work, str(i))
        if not local:
            erros_dbx.append(f"extrair pág {pg['page']} de {os.path.basename(pg['src'])}"); continue
        itens_dbx.append({"local": local, "categoria": pg["cat"], "nome_destino": _nota_nome_pdf(pg)})
    ok_r, erros_r = dropbox_rateio.ratear(access, itens_dbx)

    # mensagem
    msg = [f"✅ {len(feitos)} orçamento(s) gerado(s) e enviado(s) ao Dropbox."]
    for d in feitos: msg.append(f"• Ticket {d['ticket']} — {d['loja']} — {d['total']}" + ("" if d['pdf_ok'] else "  (⚠️ PDF falhou)"))
    msg.append(f"📁 {ok_r} nota(s) roteada(s) no Dropbox.")
    if duplicadas: msg.append(f"↩️ {duplicadas} nota(s) já processada(s) neste mês (ignoradas pelo número da nota).")
    if sem_ticket: msg.append(f"⚠️ {len(sem_ticket)} SEM TICKET (planilha de correção atualizada).")
    if diag:
        msg.append(f"🔎 Leitor: {'GROQ ' + GROQ_MODEL if GROQ_KEY else 'GEMINI ' + (_GEMINI_OK or GEMINI_MODEL)}")
    for x in diag[:5]:
        det = x.get("err") and f" | ERRO: {x['err']}" or ""
        msg.append(f"   🔎 forn='{x['forn']}' | itens={x.get('nitens')} | obs=«{x['obs']}»{det}")
    if nao_assoc:  msg.append(f"⚠️ {len(nao_assoc)} TICKET NÃO ASSOCIADO (planilha de correção atualizada).")
    if ctrl_ok:    msg.append("🧾 Planilha de controle do mês atualizada no Dropbox — baixe abaixo p/ enviar ao cliente.")
    errs = erros_dbx + erros_r
    if errs: msg.append("⚠️ Erros no Dropbox: " + "; ".join(errs[:4]))
    return (ctrl if ctrl_ok else None), "\n".join(msg)

# ---------- 2ª passagem: ORÇAMENTOS CORRIGIDOS (lê a planilha de pendentes no Dropbox) ----------
def _pendentes_list(xlsx_local):
    out = subprocess.run(["python3", os.path.join(SCRIPTS, "pendentes.py"), "list", "--xlsx", xlsx_local],
                         capture_output=True, text=True)
    try: return json.loads(out.stdout.strip() or "[]")
    except Exception: return []

def _acha_nota_pdf(arqs, numero):
    """Casa o nº da nota com um arquivo da pasta (ex.: 9107 -> 'TICKET 126486 - NOTA 9107.pdf')."""
    n = re.sub(r"\D", "", str(numero or ""))
    if not n: return None
    cands = [a for a in arqs if n in re.sub(r"\D", "", a)]
    # prefere o que casa o número exato entre separadores
    exatos = [a for a in cands if re.search(rf"(?<!\d){n}(?!\d)", a)]
    return (exatos or cands or [None])[0]

def processar_corrigidos(subpasta="TICKET NAO ASSOCIADO", arq_xlsx="NOTAS - TICKET NAO ASSOCIADO.xlsx"):
    """Gera os orçamentos das linhas RODAR=SIM da planilha de pendentes, roteia as
    notas p/ NOTAS INCLUIDAS ORCAMENTO/<classe> e remove as linhas processadas."""
    if not (GEMINI_KEY or GROQ_KEY):
        return None, "⚠️ Configure GEMINI_API_KEY (ou GROQ_API_KEY) no Render."
    if not (SB_URL and SB_KEY):
        return None, "⚠️ Faltam SUPABASE_URL / SUPABASE_SERVICE_KEY."
    if not dropbox_rateio.ativo():
        return None, "⚠️ Faltam os segredos do Dropbox."
    access = dropbox_rateio.obter_token()
    B = dropbox_rateio.BASE
    work = tempfile.mkdtemp(prefix="corr_")
    hoje = datetime.date.today()
    mes = f"{MESES[hoje.month-1]} {hoje.year}"
    data_str = hoje.strftime("%d/%m/%Y")
    dbx_mes = f"{B}/{dropbox_rateio.ORCAMENTOS}/{mes}"
    ATUALIZAR = os.path.join(SCRIPTS, "atualizar_planilha_mensal.py")
    PENDENTES = os.path.join(SCRIPTS, "pendentes.py")
    erros = []

    pasta_notas = f"{B}/{subpasta}"
    xlsx_dbx = f"{pasta_notas}/{arq_xlsx}"
    xlsx_local = os.path.join(work, arq_xlsx)
    raw = dropbox_rateio.baixar(access, xlsx_dbx)
    if not raw:
        return None, f"⚠️ Planilha não encontrada: {xlsx_dbx}"
    open(xlsx_local, "wb").write(raw)
    linhas = _pendentes_list(xlsx_local)
    if not linhas:
        return None, "Nenhuma linha com RODAR = SIM na planilha de pendentes."
    arqs = dropbox_rateio.listar(access, pasta_notas)

    # planilha de controle do mês (append com dedup por ticket)
    ctrl = os.path.join(work, f"ORÇAMENTOS MONTADOS - {mes}.xlsx")
    ctrl_dbx = f"{dbx_mes}/ORÇAMENTOS MONTADOS - {mes}.xlsx"
    try:
        a = dropbox_rateio.baixar(access, ctrl_dbx)
        if a: open(ctrl, "wb").write(a)
    except Exception as e: erros.append(f"baixar controle: {e}")

    # 1) agrupa por ticket (várias notas do mesmo ticket -> 1 orçamento)
    por_ticket, notas_por_ticket, achadas = {}, {}, []
    faltando = []
    for row in linhas:
        ticket = re.sub(r"\D", "", str(row.get("ticket") or ""))
        if not ticket:
            faltando.append(f"nota {row.get('nota')}: sem ticket na planilha"); continue
        nome_arq = _acha_nota_pdf(arqs, row.get("nota"))
        if not nome_arq:
            faltando.append(f"nota {row.get('nota')}: PDF não encontrado na pasta"); continue
        pdf_bytes = dropbox_rateio.baixar(access, f"{pasta_notas}/{nome_arq}")
        if not pdf_bytes:
            faltando.append(f"nota {row.get('nota')}: falha ao baixar"); continue
        local_pdf = os.path.join(work, nome_arq)
        open(local_pdf, "wb").write(pdf_bytes)
        # lê os itens da nota (todas as páginas)
        itens, forma = [], None
        for idx, png in paginas_imagens(local_pdf, work):
            try:
                nota = ler_nota(png)
            except Exception as e:
                nota = {"itens": [], "_erro": str(e)}
            itens.extend(nota.get("itens") or [])
            if nota.get("forma_pagamento") and not forma: forma = nota.get("forma_pagamento")
        if not itens:
            faltando.append(f"nota {row.get('nota')} (ticket {ticket}): não consegui ler itens"); continue
        # chamado: Supabase; senão usa a Loja preenchida na planilha
        ch = busca_chamado(ticket) or {"loja": row.get("loja") or "", "aba": None, "descricao": ""}
        g = por_ticket.setdefault(ticket, {"chamado": ch, "itens": [], "forma": forma})
        g["itens"].extend(itens)
        if forma and not g["forma"]: g["forma"] = forma
        notas_por_ticket.setdefault(ticket, []).append({"nota": row.get("nota"), "arq": nome_arq, "aba": ch.get("aba")})
        achadas.append(row.get("nota"))

    # 2) gera 1 orçamento por ticket + sobe PDFs + atualiza controle
    feitos = []
    for ticket, g in por_ticket.items():
        try:
            d = gera_orcamento(ticket, g["chamado"], g["itens"], work, work, data_str, g["forma"])
        except Exception as e:
            erros.append(f"orçamento ticket {ticket}: {e}"); continue
        feitos.append(d)
        if d["pdf_ok"]:
            subprocess.run(["python3", ATUALIZAR, "--xlsx", ctrl, "--ticket", str(ticket),
                            "--loja", d["nome_loja"], "--pdf", d["pdf"], "--data", data_str, "--append"],
                           capture_output=True)
            nome_pdf = d["base"] + ".pdf"
            try: dropbox_rateio.subir(access, d["pdf"], f"{dbx_mes}/{d['num']}_{slug(d['nome_loja'])}/{nome_pdf}")
            except Exception as e: erros.append(f"PDF {ticket}: {e}")
            try: dropbox_rateio.subir(access, d["pdf"], f"{B}/{dropbox_rateio.NAO_LANCADOS}/{nome_pdf}")
            except Exception as e: erros.append(f"não lançados {ticket}: {e}")

    # 3) sobe controle atualizado
    if os.path.exists(ctrl):
        try: dropbox_rateio.subir(access, ctrl, ctrl_dbx, overwrite=True)
        except Exception as e: erros.append(f"subir controle: {e}")

    # 4) MOVE cada nota p/ NOTAS INCLUIDAS ORCAMENTO/<classe> (só dos tickets que viraram orçamento)
    tickets_ok = {d["ticket"] for d in feitos}
    movidas = 0
    incl = "NOTAS INCLUIDAS ORCAMENTO"
    for ticket, itens_n in notas_por_ticket.items():
        if ticket not in tickets_ok: continue
        for it in itens_n:
            classe = (it.get("aba") or "SEM CLASSIFICACAO").upper()
            destino_pasta = f"{B}/{incl}/{classe}"
            dropbox_rateio.criar_pasta(access, destino_pasta)
            try:
                dropbox_rateio.mover(access, f"{pasta_notas}/{it['arq']}", f"{destino_pasta}/{it['arq']}")
                movidas += 1
            except Exception as e:
                erros.append(f"mover nota {it['nota']}: {e}")

    # 5) remove da planilha de pendentes só as notas que viraram orçamento; sobe a planilha
    notas_feitas = [it["nota"] for t in tickets_ok for it in notas_por_ticket.get(t, [])]
    for n in notas_feitas:
        subprocess.run(["python3", PENDENTES, "remove", "--xlsx", xlsx_local, "--nota", str(n)],
                       capture_output=True)
    if notas_feitas and os.path.exists(xlsx_local):
        try: dropbox_rateio.subir(access, xlsx_local, xlsx_dbx, overwrite=True)
        except Exception as e: erros.append(f"subir pendentes: {e}")

    # mensagem
    msg = [f"✅ {len(feitos)} orçamento(s) corrigido(s) gerado(s) e enviado(s) ao Dropbox."]
    for d in feitos: msg.append(f"• Ticket {d['ticket']} — {d['loja']} — {d['total']}" + ("" if d['pdf_ok'] else "  (⚠️ PDF falhou)"))
    msg.append(f"📁 {movidas} nota(s) movida(s) p/ NOTAS INCLUIDAS ORCAMENTO.")
    msg.append(f"🧾 {len(notas_feitas)} linha(s) removida(s) da planilha de pendentes.")
    if faltando:
        msg.append("⚠️ Ficaram de fora: " + "; ".join(faltando[:8]))
    if erros:
        msg.append("⚠️ Erros: " + "; ".join(erros[:6]))
    return (ctrl if os.path.exists(ctrl) else None), "\n".join(msg)

# ---------- FrotaHub: auth por token do Supabase + permissão por rotina ----------
def _oc_pdf_texto(pdf_bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes); p = f.name
    try:
        out = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, text=True, timeout=60)
        return out.stdout or ""
    except Exception:
        return ""
    finally:
        try: os.unlink(p)
        except Exception: pass

def _valor_br(s):
    t = re.sub(r"[^\d,.\-]", "", str(s or ""))
    if not t: return None
    if "," in t: t = t.replace(".", "").replace(",", ".")
    try: return round(float(t), 2)
    except Exception: return None

FART_BASE = "03720882"
# rótulos que aparecem no bloco de dados (usados p/ não colar o endereço no nome)
_OC_LABELS = ("Nome:", "CNPJ:", "Telefone:", "Vendedor:", "E-mail:", "Endereço:", "Endereco:", "I.E.:")

def extrai_oc(txt):
    """Extrai os campos de uma O.C. do Obra Prima (mesma lógica do PCO manual):
    lê os BLOCOS 'DADOS DO FATURAMENTO' (tomador Fartura) e 'DADOS DO FORNECEDOR'
    (o fornecedor de verdade — Nome + CNPJ), não um CNPJ solto qualquer."""
    d = {"numero":None,"data_oc":None,"centro_custo":None,"cnpj_centro_custo":None,
         "fornecedor":None,"cnpj_fornecedor":None,"valor":None}
    # número da O.C.
    m = re.search(r"ORDEM DE COMPRA\s+(\d+)", txt, re.I)
    if not m: m = re.search(r"(?:ordem de compra|o\.?c\.?)\D{0,12}(\d{4,7})", txt, re.I)
    if m: d["numero"] = m.group(1).zfill(6)
    # data (preferir Criação/Data; senão a 1ª data do documento)
    m = re.search(r"(?:cria[çc][ãa]o|data)\D{0,12}(\d{2}/\d{2}/\d{4})", txt, re.I) or re.search(r"(\d{2}/\d{2}/\d{4})", txt)
    if m:
        p=m.group(1).split("/"); d["data_oc"]=f"{p[2]}-{p[1]}-{p[0]}"
    # centro de custo (delimitado por CNO:)
    m = re.search(r"OBRA\s*/?\s*CENTRO DE CUSTO\s*:?\s*(.+?)\s{2,}CNO:", txt, re.I)
    if not m: m = re.search(r"OBRA\s*/?\s*CENTRO DE CUSTO\s*:?\s*(.+)", txt, re.I)
    if m: d["centro_custo"] = re.sub(r"\s+"," ",m.group(1)).strip()[:120]
    # bloco FATURAMENTO (tomador Fartura) -> CNPJ do centro de custo
    ftb = re.search(r"DADOS DO FATURAMENTO(.*?)DADOS DO FORNECEDOR", txt, re.DOTALL|re.I)
    if ftb:
        mfc = re.search(r"CNPJ:\s*([\d./\-]+)", ftb.group(1))
        if mfc and mfc.group(1).strip(): d["cnpj_centro_custo"] = fmt_cnpj(mfc.group(1))
    # bloco FORNECEDOR -> Nome + CNPJ do fornecedor de verdade
    fb = re.search(r"DADOS DO FORNECEDOR(.*?)(?:OBRA\s*/?\s*CENTRO DE CUSTO|DADOS DO|ITENS|PRODUTOS)", txt, re.DOTALL|re.I) \
         or re.search(r"DADOS DO FORNECEDOR(.*)", txt, re.DOTALL|re.I)
    if fb:
        block = fb.group(1); lines = block.splitlines()
        for i, line in enumerate(lines):
            mn = re.search(r"Nome:\s+(.+?)(?:\s{2,}Endere[çc]o:.*)?$", line)
            if mn:
                nome = mn.group(1).strip()
                if i+1 < len(lines):
                    nxt = lines[i+1].strip()
                    if nxt and not any(lbl in lines[i+1] for lbl in _OC_LABELS):
                        nome = (nome + " " + re.sub(r"\s{2,}.*$","",nxt)).strip()
                d["fornecedor"] = re.sub(r"\s+"," ",nome).strip()[:160]
                break
        mc = re.search(r"CNPJ:\s*([\d./\-]*)", block)
        if mc and mc.group(1).strip(): d["cnpj_fornecedor"] = fmt_cnpj(mc.group(1))
    # total
    totais = re.findall(r"(?mi)^\s*Total\s+([\d.,]+)\s*$", txt)
    if totais: d["valor"] = _valor_br(totais[-1])
    else:
        m = re.search(r"(?:total geral|valor total|total)\s*[:\-]?\s*R?\$?\s*([\d\.\,]+)", txt, re.I)
        if m: d["valor"] = _valor_br(m.group(1))
    # validação PCO
    fat_ok = bool(d["cnpj_centro_custo"]) and re.sub(r"\D","",d["cnpj_centro_custo"] or "").startswith(FART_BASE)
    d["valido"] = bool(d["cnpj_fornecedor"]) and fat_ok
    d["motivo"] = "" if d["valido"] else ("sem CNPJ do fornecedor" if not d["cnpj_fornecedor"] else "faturamento não é Fartura")
    return d

def _bearer(request):
    h = request.headers.get("authorization","") or request.headers.get("Authorization","")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""

def _sb_json(url, key, tok=None, data=None, method="GET"):
    hdrs = {"apikey": key, "authorization": f"Bearer {tok or key}"}
    if data is not None: hdrs["content-type"]="application/json"
    req = urllib.request.Request(url, data=(json.dumps(data).encode() if data is not None else None),
                                 headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        b=r.read().decode()
        return json.loads(b) if b else None

def _sb_delete(url):
    req = urllib.request.Request(url, method="DELETE",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "prefer": "return=minimal"})
    try: urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e: print("sb delete erro:", e.read().decode()[:200], flush=True)

def auth_user(tok):
    if not tok: return None
    try: return _sb_json(f"{SB_URL}/auth/v1/user", SB_ANON, tok)
    except Exception: return None

def perfil_de(uid):
    q=urllib.parse.urlencode({"id":f"eq.{uid}","select":"papel,nome,ativo","limit":"1"})
    try:
        d=_sb_json(f"{SB_URL}/rest/v1/perfis?{q}", SB_KEY)
        return d[0] if d else None
    except Exception: return None

def pode_rotina(papel, rotina):
    if papel=="builder": return True
    q=urllib.parse.urlencode({"papel":f"eq.{papel}","rotina":f"eq.{rotina}","pode":"is.true","select":"rotina","limit":"1"})
    try:
        return bool(_sb_json(f"{SB_URL}/rest/v1/permissoes?{q}", SB_KEY))
    except Exception: return False

def exige(request, rotina):
    """Valida o token do FrotaHub e a permissão. Devolve (user, perfil) ou levanta HTTPException."""
    from fastapi import HTTPException
    u=auth_user(_bearer(request))
    if not u or not u.get("id"): raise HTTPException(401, "não autenticado")
    p=perfil_de(u["id"])
    if not p or p.get("ativo") is False: raise HTTPException(403, "usuário sem perfil ativo")
    if not pode_rotina(p["papel"], rotina): raise HTTPException(403, f"sem permissão para {rotina}")
    return u, p

def log_frotahub(uid, papel, rotina, acao, alvo="", detalhe=None):
    try:
        _sb_json(f"{SB_URL}/rest/v1/log_atividades", SB_KEY, data={
            "user_id":uid,"papel":papel,"rotina":rotina,"acao":acao,"alvo":alvo,
            "detalhe":detalhe or {}}, method="POST")
    except Exception: pass

# ---------- interface (FastAPI puro — sem Gradio) ----------
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Motor de Orçamentos — Frota Macedo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
RESULTS = {}   # token -> caminho da planilha de controle p/ download

PAGINA = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Motor de Orçamentos — Frota Macedo</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:640px;margin:28px auto;padding:0 18px;color:#1e2733}
 h1{font-size:21px;color:#1F3864} .card{border:1px solid #C9CFDA;border-radius:12px;padding:20px;background:#fff}
 label{font-weight:bold;display:block;margin:14px 0 6px} input[type=file]{width:100%}
 button{margin-top:18px;background:#1F3864;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-size:16px;cursor:pointer}
 button:disabled{background:#9aa4b4} small{color:#5A6472} #msg{white-space:pre-wrap;margin-top:16px}
 .ok{color:#1a7f37} .err{color:#b3261e} a.dl{display:inline-block;margin-top:14px;background:#1a7f37;color:#fff;padding:11px 18px;border-radius:8px;text-decoration:none}
</style></head><body>
<h1>Motor de Orçamentos — Frota Macedo</h1>
<div class="card">
 <p><small>Suba as notas/DAVs. O motor gera os orçamentos, envia tudo para o seu Dropbox
 (orçamentos, notas roteadas, planilhas) e deixa a <b>planilha de controle do mês</b> pronta para baixar e enviar ao cliente.</small></p>
 <form id="f">
  <label>Notas / DAVs (PDF ou imagem — pode selecionar várias)</label>
  <input type="file" name="notas" multiple accept=".pdf,.jpg,.jpeg,.png" required>
  <button id="b" type="submit">Gerar e enviar ao Dropbox</button>
 </form>
 <hr style="margin:22px 0;border:0;border-top:1px solid #E1E5EC">
 <p><small><b>Orçamentos corrigidos</b> — depois de corrigir o Ticket/Loja e marcar
  <b>RODAR = SIM</b> na planilha <i>NOTAS - TICKET NAO ASSOCIADO</i> (no Dropbox),
  clique abaixo. O motor gera os orçamentos dessas notas, roteia e remove as linhas.</small></p>
 <button id="bc" type="button" style="background:#1a7f37">Rodar corrigidos (TICKET NÃO ASSOCIADO)</button>
 <div id="msg"></div>
</div>
<script>
const f=document.getElementById('f'), b=document.getElementById('b'), msg=document.getElementById('msg');
const bc=document.getElementById('bc');
bc.addEventListener('click', async ()=>{
 bc.disabled=true; bc.textContent='Processando… (pode levar 1–2 min)'; msg.textContent='';
 try{
  const r=await fetch('corrigidos',{method:'POST'});
  const j=await r.json();
  if(j.erro){ msg.innerHTML='<span class="err">'+j.erro+'</span>'; }
  else{
   const warn=(j.status||'').trim().indexOf('⚠️')===0;
   let h='<span class="'+(warn?'err':'ok')+'">'+(j.status||'').replace(/</g,'&lt;')+'</span>';
   if(j.token){ h+='<br><a class="dl" href="baixar?t='+j.token+'">⬇ Baixar planilha de controle</a>'; }
   msg.innerHTML=h;
  }
 }catch(err){ msg.innerHTML='<span class="err">Falhou: '+err+'</span>'; }
 bc.disabled=false; bc.textContent='Rodar corrigidos (TICKET NÃO ASSOCIADO)';
});
f.addEventListener('submit', async (e)=>{
 e.preventDefault(); b.disabled=true; b.textContent='Processando… (pode levar 1–2 min)'; msg.textContent='';
 try{
  const r=await fetch('processar',{method:'POST',body:new FormData(f)});
  const j=await r.json();
  if(j.erro){ msg.innerHTML='<span class="err">'+j.erro+'</span>'; }
  else{
   const warn=(j.status||'').trim().indexOf('⚠️')===0;
   let h='<span class="'+(warn?'err':'ok')+'">'+(j.status||'').replace(/</g,'&lt;')+'</span>';
   if(j.token){ h+='<br><a class="dl" href="baixar?t='+j.token+'">⬇ Baixar planilha de controle</a>'; }
   msg.innerHTML=h;
  }
 }catch(err){ msg.innerHTML='<span class="err">Falhou: '+err+'</span>'; }
 b.disabled=false; b.textContent='Gerar e enviar ao Dropbox';
});
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGINA

@app.post("/processar")
async def processar_endpoint(request: Request):
    form = await request.form()
    tmp = tempfile.mkdtemp(prefix="up_")
    caminhos = []
    for uf in form.getlist("notas"):
        if not getattr(uf, "filename", ""): continue
        dest = os.path.join(tmp, os.path.basename(uf.filename))
        with open(dest, "wb") as w: w.write(await uf.read())
        caminhos.append(dest)
    if not caminhos:
        return {"erro": "Selecione ao menos uma nota."}
    try:
        path, status = processar(caminhos)
    except Exception as e:
        return {"erro": f"Erro ao processar: {e}"}
    token = None
    if path and os.path.exists(path):
        import secrets as _s
        token = _s.token_urlsafe(10)
        RESULTS[token] = path
    return {"status": status, "token": token}

@app.post("/corrigidos")
def corrigidos_endpoint():
    try:
        path, status = processar_corrigidos()
    except Exception as e:
        return {"erro": f"Erro ao processar corrigidos: {e}"}
    token = None
    if path and os.path.exists(path):
        import secrets as _s
        token = _s.token_urlsafe(10)
        RESULTS[token] = path
    return {"status": status, "token": token}

@app.get("/baixar")
def baixar(t: str):
    path = RESULTS.get(t)
    if not path or not os.path.exists(path):
        return PlainTextResponse("Arquivo expirado — gere novamente.", status_code=404)
    return FileResponse(path, filename=os.path.basename(path),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ============ API do FrotaHub (protegida por token do Supabase) ============
@app.get("/api/ping")
def api_ping(): return {"ok": True, "motor": "frotahub", "rev": 60}

@app.get("/api/me")
def api_me(request: Request):
    from fastapi import HTTPException
    u=auth_user(_bearer(request))
    if not u: raise HTTPException(401,"não autenticado")
    p=perfil_de(u["id"]) or {}
    return {"email":u.get("email"),"nome":p.get("nome"),"papel":p.get("papel")}

PCO_ENVIAR = PCO_BASE + "/0 - ENVIAR (ADICIONAR AQUI)"
PCO_RESIDUAL = PCO_BASE + "/100 - ARQUIVOS RESIDUAIS (NÃO MEXER)"

@app.post("/pco/listar")
def pco_listar(request: Request):
    """CONFERIR_LISTA_PCO — lê a pasta 0-ENVIAR, extrai os dados de cada O.C. e
    devolve a lista (filtra as que já têm PCO enviado no banco)."""
    from fastapi import HTTPException
    u,p = exige(request, "CONFERIR_LISTA_PCO")
    if not dropbox_rateio.ativo(): raise HTTPException(500,"Dropbox não configurado")
    access = dropbox_rateio.obter_token()
    arqs = [a for a in dropbox_rateio.listar(access, PCO_ENVIAR) if a.lower().endswith(".pdf")]
    # números de O.C. já enviadas (não mostrar de novo)
    try:
        env = _sb_json(f"{SB_URL}/rest/v1/ocs?pco_status=eq.enviado&select=numero", SB_KEY) or []
        ja = {str(r["numero"]) for r in env}
    except Exception: ja=set()
    itens=[]
    for nome in sorted(arqs):
        try:
            pdf = dropbox_rateio.baixar(access, f"{PCO_ENVIAR}/{nome}")
            d = extrai_oc(_oc_pdf_texto(pdf)) if pdf else {}
        except Exception as e:
            d = {"_erro": str(e)[:120]}
        if d.get("numero") and d["numero"] in ja:   # já enviada
            continue
        d["arquivo"]=nome
        itens.append(d)
    return {"pasta": PCO_ENVIAR, "total": len(itens), "itens": itens}

@app.post("/pco/arquivos")
def pco_arquivos(request: Request):
    """Lista rápida só com os NOMES dos PDFs da pasta 0-ENVIAR (p/ barra de progresso)."""
    from fastapi import HTTPException
    exige(request, "CONFERIR_LISTA_PCO")
    if not dropbox_rateio.ativo(): raise HTTPException(500,"Dropbox não configurado")
    access = dropbox_rateio.obter_token()
    arqs = [a for a in dropbox_rateio.listar(access, PCO_ENVIAR) if a.lower().endswith(".pdf")]
    return {"arquivos": sorted(arqs)}

@app.get("/pco/oc")
def pco_oc(request: Request, arquivo: str):
    """Lê UMA O.C. (extrai os campos) — usado no carregamento com progresso."""
    from fastapi import HTTPException
    exige(request, "CONFERIR_LISTA_PCO")
    access = dropbox_rateio.obter_token()
    nome = os.path.basename(arquivo)
    pdf = dropbox_rateio.baixar(access, f"{PCO_ENVIAR}/{nome}")
    d = extrai_oc(_oc_pdf_texto(pdf)) if pdf else {}
    d["arquivo"] = nome
    d["ja_enviada"] = False
    if d.get("numero"):
        try:
            q = urllib.parse.urlencode({"numero": f"eq.{d['numero']}", "pco_status":"eq.enviado","select":"numero","limit":"1"})
            d["ja_enviada"] = bool(_sb_json(f"{SB_URL}/rest/v1/ocs?{q}", SB_KEY))
        except Exception: pass
    return d

@app.get("/pco/visualizar")
def pco_visualizar(request: Request, arquivo: str):
    from fastapi import HTTPException
    exige(request, "CONFERIR_LISTA_PCO")
    access = dropbox_rateio.obter_token()
    pdf = dropbox_rateio.baixar(access, f"{PCO_ENVIAR}/{os.path.basename(arquivo)}")
    if not pdf: raise HTTPException(404,"arquivo não encontrado")
    return Response(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{os.path.basename(arquivo)}"'})

@app.post("/pco/excluir")
async def pco_excluir(request: Request):
    from fastapi import HTTPException
    u,p = exige(request, "CONFERIR_LISTA_PCO")
    body = await request.json()
    arq = os.path.basename(body.get("arquivo",""))
    if not arq: raise HTTPException(400,"arquivo?")
    access = dropbox_rateio.obter_token()
    try:
        dropbox_rateio.apagar(access, f"{PCO_ENVIAR}/{arq}")   # vai p/ lixeira do Dropbox (recuperável)
    except Exception as e:
        raise HTTPException(500, f"falha ao excluir: {e}")
    log_frotahub(u["id"], p["papel"], "CONFERIR_LISTA_PCO", "EXCLUIU_OC", arq)
    return {"ok": True}

@app.post("/pco/reenviar")
async def pco_reenviar(request: Request):
    """Libera uma O.C. já enviada para ser enviada de novo: APAGA o registro de envio
    (numero) no banco. O PDF continua em 0-ENVIAR e volta a aparecer na lista normal."""
    from fastapi import HTTPException
    u,p = exige(request, "ENVIAR_PCO")           # reabrir um envio exige permissão de envio
    body = await request.json()
    numero = str(body.get("numero","")).strip()
    if not numero: raise HTTPException(400,"numero?")
    req = urllib.request.Request(
        f'{SB_URL}/rest/v1/ocs?numero=eq.{urllib.parse.quote(numero)}&pco_status=eq.enviado',
        method="DELETE",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "prefer": "return=minimal"})
    try: urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e: raise HTTPException(500, "apagar registro: " + e.read().decode()[:150])
    log_frotahub(u["id"], p["papel"], "ENVIAR_PCO", "REENVIAR_LIBEROU", numero)
    return {"ok": True, "numero": numero}

# ---------------- OC_INVALIDA (O.C. bloqueadas) ----------------
def _sugerir_cnpj_cc(centro):
    """Sugere o CNPJ de faturamento certo (Fartura) para um centro de custo,
    olhando outra O.C. do mesmo centro que já tenha CNPJ válido."""
    if not centro: return None
    try:
        q = urllib.parse.urlencode({"centro_custo": f"eq.{centro}",
            "cnpj_centro_custo": f"like.{FART_BASE}*", "select": "cnpj_centro_custo", "limit": "1"})
        r = _sb_json(f"{SB_URL}/rest/v1/ocs?{q}", SB_KEY) or []
        return r[0]["cnpj_centro_custo"] if r else None
    except Exception: return None

def _bloq_row(numero):
    q = urllib.parse.urlencode({"numero": f"eq.{numero}", "pco_status": "eq.bloqueado", "limit": "1",
        "select": "numero,data_oc,centro_custo,arquivo_oc_path"})
    r = _sb_json(f"{SB_URL}/rest/v1/ocs?{q}", SB_KEY) or []
    return r[0] if r else None

def _pdf_carimbo_correcao(pdf_bytes, linhas):
    """Carimba um banner de correção no topo da 1ª página do PDF."""
    import io as _io
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import HexColor
    reader = PdfReader(_io.BytesIO(pdf_bytes))
    page0 = reader.pages[0]
    w = float(page0.mediabox.width); h = float(page0.mediabox.height)
    bh = 22 + 12 * len(linhas)
    buf = _io.BytesIO(); c = canvas.Canvas(buf, pagesize=(w, h))
    c.setFillColor(HexColor("#7A1517")); c.rect(0, h - bh, w, bh, fill=1, stroke=0)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont("Helvetica-Bold", 9); c.drawString(14, h - 16, "CORREÇÃO — FROTA MACEDO ENGENHARIA")
    c.setFont("Helvetica", 8); y = h - 30
    for ln in linhas:
        c.drawString(14, y, (ln or "")[:150]); y -= 12
    c.save(); buf.seek(0)
    overlay = PdfReader(buf).pages[0]
    page0.merge_page(overlay)
    writer = PdfWriter(); writer.add_page(page0)
    for pg in reader.pages[1:]: writer.add_page(pg)
    out = _io.BytesIO(); writer.write(out); return out.getvalue()

def _oc_words(pdf_bytes):
    """Palavras da 1ª página com bounding box (via pdftotext -bbox)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f: f.write(pdf_bytes); pth = f.name
    outp = pth + ".html"
    try:
        subprocess.run(["pdftotext", "-bbox", "-f", "1", "-l", "1", pth, outp], capture_output=True, timeout=30)
        h = open(outp, encoding="utf-8").read()
    except Exception: return [], 595.0, 842.0
    finally:
        for x in (pth, outp):
            try: os.unlink(x)
            except Exception: pass
    mp = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', h)
    pageW, pageH = (float(mp.group(1)), float(mp.group(2))) if mp else (595.0, 842.0)
    words = [(float(a), float(b), float(c), float(d), w) for a, b, c, d, w in
             re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', h)]
    return words, pageW, pageH

def corrige_oc_inplace(pdf_bytes, campos):
    """Cobre o valor ERRADO e escreve o CERTO no mesmo lugar (sem banner) — deixa a O.C. com
    aparência de original. campos: cnpj_centro_custo, fornecedor, cnpj_fornecedor, endereco_fornecedor.
    Retorna bytes do PDF corrigido, ou None se o layout não bater (aí o chamador usa o original)."""
    import io as _io
    from reportlab.pdfgen import canvas as _cv
    from reportlab.lib.colors import white as _wh, black as _bk
    try:
        words, pageW, pageH = _oc_words(pdf_bytes)
    except Exception:
        return None
    def yhdr(nm):
        for x0, y0, x1, y1, w in words:
            if w == nm: return y0
        return None
    y_fat = yhdr("FATURAMENTO"); y_forn = yhdr("FORNECEDOR"); y_obra = yhdr("OBRA/CENTRO") or 9999
    if y_fat is None or y_forn is None: return None
    def find_lab(label, ylo, yhi, xmin=0, xmax=250):
        c = [(x0, y0, x1, y1) for x0, y0, x1, y1, w in words if w == label and ylo < y0 < yhi and xmin <= x0 < xmax]
        return c[0] if c else None
    ovl = []
    def esq(lab, txt): ovl.append((lab[2] + 2, 350.0, lab, txt))
    def dire(lab, txt): ovl.append((lab[2] + 3, pageW - 28, lab, txt))
    if campos.get("cnpj_centro_custo"):
        lab = find_lab("CNPJ:", y_fat, y_forn, xmax=160)
        if lab: esq(lab, campos["cnpj_centro_custo"])
    if campos.get("fornecedor"):
        lab = find_lab("Nome:", y_forn, y_obra, xmax=160)
        if lab: esq(lab, campos["fornecedor"])
    if campos.get("cnpj_fornecedor"):
        lab = find_lab("CNPJ:", y_forn, y_obra, xmax=160)
        if lab: esq(lab, campos["cnpj_fornecedor"])
    if campos.get("endereco_fornecedor"):
        lab = find_lab("Endereço:", y_forn, y_obra, xmin=340, xmax=430)
        if lab: dire(lab, campos["endereco_fornecedor"])
    if not ovl: return None
    buf = _io.BytesIO(); c = _cv.Canvas(buf, pagesize=(pageW, pageH))
    for cx0, cx1, lab, txt in ovl:
        lx0, ly0, lx1, ly1 = lab
        c.setFillColor(_wh); c.rect(cx0, pageH - ly1 - 1.5, cx1 - cx0, (ly1 - ly0) + 4.5, stroke=0, fill=1)
        c.setFillColor(_bk); c.setFont("Helvetica", 8.5)
        c.drawString(cx0 + 1, pageH - ly1 + 1.2, str(txt))
    c.save(); buf.seek(0)
    ov = PdfReader(buf).pages[0]
    reader = PdfReader(_io.BytesIO(pdf_bytes)); p0 = reader.pages[0]; p0.merge_page(ov)
    wr = PdfWriter(); wr.add_page(p0)
    for pg in reader.pages[1:]: wr.add_page(pg)
    out = _io.BytesIO(); wr.write(out); return out.getvalue()

@app.get("/pco/bloqueadas")
def pco_bloqueadas(request: Request):
    from fastapi import HTTPException
    exige(request, "OC_INVALIDA")
    q = urllib.parse.urlencode({"pco_status": "eq.bloqueado", "order": "numero",
        "select": "numero,data_oc,centro_custo,cnpj_centro_custo,fornecedor,cnpj_fornecedor,valor,endereco_fornecedor,bloqueio_motivo,bloqueio_em,arquivo_oc_path"})
    try: rows = _sb_json(f"{SB_URL}/rest/v1/ocs?{q}", SB_KEY) or []
    except Exception as e: raise HTTPException(500, f"ler bloqueadas: {e}")
    for r in rows:
        cc = re.sub(r"\D", "", r.get("cnpj_centro_custo") or "")
        if not cc.startswith(FART_BASE):
            r["cnpj_cc_sugerido"] = _sugerir_cnpj_cc(r.get("centro_custo"))
    return {"itens": rows, "total": len(rows)}

@app.get("/pco/bloqueada_pdf")
def pco_bloqueada_pdf(request: Request, numero: str):
    from fastapi import HTTPException
    exige(request, "OC_INVALIDA")
    row = _bloq_row(numero)
    if not row or not row.get("arquivo_oc_path"): raise HTTPException(404, "O.C. não encontrada")
    access = dropbox_rateio.obter_token()
    pdf = dropbox_rateio.baixar(access, f"{PCO_BASE}/{row['arquivo_oc_path']}")
    if not pdf: raise HTTPException(404, "PDF não encontrado")
    return Response(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{numero}.pdf"'})

def _oc_corrigir_aplicar(access, numero, campos, orig_pdf, destino_nome):
    """Valida os campos, carimba o PDF, sobe em 0-ENVIAR/destino_nome (overwrite) e grava o
    registro pendente+corrigida (que o envio passa a usar no lugar do PDF). Retorna (ok, motivo)."""
    cc_cnpj  = fmt_cnpj(campos.get("cnpj_centro_custo"))
    forn     = (campos.get("fornecedor") or "").strip()
    forn_cnpj= fmt_cnpj(campos.get("cnpj_fornecedor"))
    endereco = (campos.get("endereco_fornecedor") or "").strip()
    centro   = (campos.get("centro_custo") or "").strip()
    valor    = _valor_br(campos.get("valor")) if str(campos.get("valor") or "").strip() else None
    fat_ok  = bool(cc_cnpj) and re.sub(r"\D","",cc_cnpj).startswith(FART_BASE)
    forn_ok = len(re.sub(r"\D","",forn_cnpj or "")) == 14
    if not (fat_ok and forn_ok):
        m = []
        if not fat_ok:  m.append("CNPJ de faturamento precisa começar com 03.720.882")
        if not forn_ok: m.append("CNPJ do fornecedor inválido/ausente")
        return False, "; ".join(m)
    if not orig_pdf: return False, "PDF da O.C. não encontrado"
    # correção in-place: cobre o valor errado e escreve o certo no mesmo lugar (O.C. fica com cara de original)
    campos_ov = {"cnpj_centro_custo": cc_cnpj, "fornecedor": forn,
                 "cnpj_fornecedor": forn_cnpj, "endereco_fornecedor": endereco or None}
    corrig = corrige_oc_inplace(orig_pdf, campos_ov) or orig_pdf
    dropbox_rateio.criar_pasta(access, PCO_ENVIAR)
    dropbox_rateio.subir_bytes(access, corrig, f"{PCO_ENVIAR}/{destino_nome}", overwrite=True)
    sb_upsert_ocs([{ "numero": numero, "data_oc": campos.get("data_oc"),
        "centro_custo": centro or None, "cnpj_centro_custo": cc_cnpj,
        "fornecedor": forn, "cnpj_fornecedor": forn_cnpj, "endereco_fornecedor": endereco or None,
        "valor": valor, "pco_status": "pendente", "corrigida": True, "bloqueio_motivo": None,
        "arquivo_oc_path": f"0 - ENVIAR (ADICIONAR AQUI)/{destino_nome}" }])
    return True, ""

@app.post("/pco/corrigir")
async def pco_corrigir(request: Request):
    """Correção DEPOIS do envio: O.C. já bloqueadas no banco (pasta 2 - OCS BLOQUEADAS)."""
    from fastapi import HTTPException
    u, p = exige(request, "OC_INVALIDA")
    itens = (await request.json()).get("itens") or []
    access = dropbox_rateio.obter_token()
    res = []
    for it in itens:
        numero = str(it.get("numero", "")).strip()
        if not numero: continue
        row = _bloq_row(numero)
        if not row or not row.get("arquivo_oc_path"):
            res.append({"numero": numero, "ok": False, "motivo": "PDF da O.C. não encontrado"}); continue
        it2 = dict(it); it2.setdefault("data_oc", row.get("data_oc")); it2.setdefault("centro_custo", row.get("centro_custo"))
        try:
            orig = dropbox_rateio.baixar(access, f"{PCO_BASE}/{row['arquivo_oc_path']}")
            # apaga o registro bloqueado; o corrigido entra como pendente+corrigida
            _sb_delete(f"{SB_URL}/rest/v1/ocs?numero=eq.{urllib.parse.quote(numero)}&pco_status=eq.bloqueado")
            ok, motivo = _oc_corrigir_aplicar(access, numero, it2, orig, f"{numero}.pdf")
            if ok:
                try: dropbox_rateio.apagar(access, f"{PCO_BASE}/{row['arquivo_oc_path']}")   # tira da pasta de bloqueadas
                except Exception: pass
            res.append({"numero": numero, "ok": ok, "motivo": motivo})
        except Exception as e:
            res.append({"numero": numero, "ok": False, "motivo": str(e)[:150]})
    ok = sum(1 for r in res if r.get("ok"))
    log_frotahub(u["id"], p["papel"], "OC_INVALIDA", "CORRIGIU_OC", f"{ok}/{len(res)} corrigidas")
    return {"resultados": res, "corrigidas": ok, "total": len(res)}

@app.post("/pco/corrigir_previa")
async def pco_corrigir_previa(request: Request):
    """Correção ANTES do envio: O.C. ainda em 0-ENVIAR (não bloqueadas no banco)."""
    from fastapi import HTTPException
    u, p = exige(request, "ENVIAR_PCO")
    itens = (await request.json()).get("itens") or []
    access = dropbox_rateio.obter_token()
    res = []
    for it in itens:
        numero  = str(it.get("numero", "")).strip()
        arquivo = os.path.basename(it.get("arquivo", "") or "")
        if not (numero and arquivo):
            res.append({"numero": numero, "ok": False, "motivo": "arquivo/número ausente"}); continue
        try:
            orig = dropbox_rateio.baixar(access, f"{PCO_ENVIAR}/{arquivo}")
            ok, motivo = _oc_corrigir_aplicar(access, numero, it, orig, arquivo)   # sobrescreve o próprio arquivo
            res.append({"numero": numero, "ok": ok, "motivo": motivo})
        except Exception as e:
            res.append({"numero": numero, "ok": False, "motivo": str(e)[:150]})
    ok = sum(1 for r in res if r.get("ok"))
    log_frotahub(u["id"], p["papel"], "ENVIAR_PCO", "CORRIGIU_PREVIA", f"{ok}/{len(res)} corrigidas")
    return {"resultados": res, "corrigidas": ok, "total": len(res)}

@app.post("/pco/bloqueada_excluir")
async def pco_bloqueada_excluir(request: Request):
    from fastapi import HTTPException
    u, p = exige(request, "OC_INVALIDA")
    numero = str((await request.json()).get("numero", "")).strip()
    if not numero: raise HTTPException(400, "numero?")
    row = _bloq_row(numero)
    access = dropbox_rateio.obter_token()
    if row and row.get("arquivo_oc_path"):
        try: dropbox_rateio.apagar(access, f"{PCO_BASE}/{row['arquivo_oc_path']}")
        except Exception: pass
    _sb_delete(f"{SB_URL}/rest/v1/ocs?numero=eq.{urllib.parse.quote(numero)}&pco_status=eq.bloqueado")
    log_frotahub(u["id"], p["papel"], "OC_INVALIDA", "EXCLUIU_BLOQUEADA", numero)
    return {"ok": True, "numero": numero}

# ================= NOTAS FISCAIS — CONFERIR_NOTA (obra) =================
@app.get("/notas/buscar_oc")
def notas_buscar_oc(request: Request, q: str = ""):
    """Busca O.C. ENVIADAS por número, fornecedor ou centro de custo (a partir de 3 letras)."""
    from fastapi import HTTPException
    exige(request, "CONFERIR_NOTA")
    q = (q or "").strip()
    if len(q) < 3: return {"itens": []}
    val = f"(numero.ilike.*{q}*,fornecedor.ilike.*{q}*,centro_custo.ilike.*{q}*)"
    url = (f"{SB_URL}/rest/v1/v_ocs?pco_status=eq.enviado&or={urllib.parse.quote(val)}"
           f"&select=id,numero,data_oc,centro_custo,fornecedor,valor,qtd_notas,valor_notas&order=numero&limit=60")
    try: rows = _sb_json(url, SB_KEY) or []
    except Exception as e: raise HTTPException(500, f"buscar: {e}")
    for r in rows:
        try: r["saldo"] = round(float(r.get("valor") or 0) - float(r.get("valor_notas") or 0), 2)
        except Exception: r["saldo"] = None
    return {"itens": rows}

@app.get("/notas/da_oc")
def notas_da_oc(request: Request, oc_id: str):
    from fastapi import HTTPException
    exige(request, "CONFERIR_NOTA")
    url = (f"{SB_URL}/rest/v1/notas?oc_id=eq.{urllib.parse.quote(oc_id)}"
           f"&select=numero_nota,emissao,valor,tem_boleto,recebimento,divergencia,entregue&order=criado_em")
    try: return {"itens": _sb_json(url, SB_KEY) or []}
    except Exception as e: raise HTTPException(500, f"notas: {e}")

@app.post("/notas/conferir")
async def notas_conferir(request: Request):
    """Gera uma N.F. ligada à O.C. (parcial ou total). Total com valor divergente exige observação."""
    from fastapi import HTTPException
    u, p = exige(request, "CONFERIR_NOTA")
    b = await request.json()
    oc_id       = str(b.get("oc_id", "")).strip()
    numero_nota = (b.get("numero_nota") or "").strip()
    emissao     = (b.get("emissao") or "").strip() or None
    valor       = _valor_br(b.get("valor")) if str(b.get("valor") or "").strip() else None
    tem_boleto  = bool(b.get("tem_boleto"))
    recebimento = b.get("recebimento") if b.get("recebimento") in ("parcial", "total") else "total"
    divergencia = (b.get("divergencia") or "").strip() or None
    if not oc_id: raise HTTPException(400, "oc_id?")
    if not (numero_nota and emissao and valor is not None):
        raise HTTPException(400, "Preencha número da nota, data de emissão e valor.")
    oc = _sb_json(f"{SB_URL}/rest/v1/ocs?id=eq.{urllib.parse.quote(oc_id)}&select=numero,valor&limit=1", SB_KEY) or []
    if not oc: raise HTTPException(404, "O.C. não encontrada")
    oc_valor, oc_num = oc[0].get("valor"), oc[0].get("numero")
    if recebimento == "total" and oc_valor is not None and abs(float(valor) - float(oc_valor)) > 0.005 and not divergencia:
        raise HTTPException(400, "Recebimento TOTAL com valor diferente da O.C.: informe a observação.")
    nota = {"oc_id": oc_id, "numero_nota": numero_nota, "emissao": emissao, "valor": valor,
            "tem_boleto": tem_boleto, "recebimento": recebimento, "divergencia": divergencia,
            "conferida": True, "conferida_por": u["id"]}
    req = urllib.request.Request(f"{SB_URL}/rest/v1/notas",
        data=json.dumps(nota, ensure_ascii=False).encode(), method="POST",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "content-type": "application/json",
                 "prefer": "return=minimal"})
    try: urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e: raise HTTPException(500, "gravar nota: " + e.read().decode()[:200])
    log_frotahub(u["id"], p["papel"], "CONFERIR_NOTA", "CONFERIU_NOTA", f"{oc_num}/NF {numero_nota} ({recebimento})")
    vo = _sb_json(f"{SB_URL}/rest/v1/v_ocs?id=eq.{urllib.parse.quote(oc_id)}&select=valor,qtd_notas,valor_notas&limit=1", SB_KEY) or []
    resumo = vo[0] if vo else {}
    if resumo:
        try: resumo["saldo"] = round(float(resumo.get("valor") or 0) - float(resumo.get("valor_notas") or 0), 2)
        except Exception: pass
    return {"ok": True, "oc_numero": oc_num, "resumo": resumo}

# ================= NOTAS FISCAIS — RECEBER_NOTA (adm) =================
@app.get("/notas/receber_lista")
def notas_receber_lista(request: Request, q: str = ""):
    """Notas já conferidas na obra e ainda NÃO entregues (via física pendente no adm)."""
    from fastapi import HTTPException
    exige(request, "RECEBER_NOTA")
    url = (f"{SB_URL}/rest/v1/notas?entregue=eq.false&conferida=eq.true&order=criado_em"
           f"&select=id,numero_nota,emissao,valor,tem_boleto,recebimento,divergencia,ocs(numero,fornecedor,centro_custo,valor)")
    try: rows = _sb_json(url, SB_KEY) or []
    except Exception as e: raise HTTPException(500, f"listar: {e}")
    ql = (q or "").strip().lower()
    out = []
    for r in rows:
        oc = r.get("ocs") or {}
        item = {"id": r.get("id"), "numero_nota": r.get("numero_nota"), "emissao": r.get("emissao"),
                "valor": r.get("valor"), "tem_boleto": r.get("tem_boleto"), "recebimento": r.get("recebimento"),
                "divergencia": r.get("divergencia"), "oc_numero": oc.get("numero"), "fornecedor": oc.get("fornecedor"),
                "centro_custo": oc.get("centro_custo"), "oc_valor": oc.get("valor")}
        if len(ql) >= 3:
            blob = " ".join(str(x or "") for x in (item["oc_numero"], item["fornecedor"], item["centro_custo"], item["numero_nota"])).lower()
            if ql not in blob: continue
        out.append(item)
    return {"itens": out[:300], "total": len(out)}

@app.post("/notas/receber")
async def notas_receber(request: Request):
    """Marca a nota como entregue (via física recebida) e grava forma de pagamento + vencimento."""
    from fastapi import HTTPException
    u, p = exige(request, "RECEBER_NOTA")
    b = await request.json()
    nota_id = str(b.get("nota_id", "")).strip()
    forma   = (b.get("forma_pagamento") or "").strip() or None
    venc    = (b.get("vencimento") or "").strip() or None
    if not nota_id: raise HTTPException(400, "nota_id?")
    if not forma:   raise HTTPException(400, "Informe a forma de pagamento.")
    patch = {"entregue": True, "entregue_em": datetime.datetime.now().isoformat(),
             "entregue_por": u["id"], "forma_pagamento": forma, "vencimento": venc}
    req = urllib.request.Request(f"{SB_URL}/rest/v1/notas?id=eq.{urllib.parse.quote(nota_id)}",
        data=json.dumps(patch, ensure_ascii=False).encode(), method="PATCH",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "content-type": "application/json",
                 "prefer": "return=minimal"})
    try: urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e: raise HTTPException(500, "receber: " + e.read().decode()[:200])
    log_frotahub(u["id"], p["papel"], "RECEBER_NOTA", "RECEBEU_NOTA", nota_id)
    return {"ok": True}

# ================= NOTAS FISCAIS — PROCURAR / PENDÊNCIAS / VENCIMENTOS =================
def _data_br(s):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else (str(s or ""))

def _lista_pdf(titulo, colunas, linhas, subtitulo="", aligns=None):
    """PDF paisagem, formatado com a identidade visual da Frota (cabeçalho maroon)."""
    import io as _io
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=1.1*cm, rightMargin=1.1*cm,
                            topMargin=1.1*cm, bottomMargin=1.1*cm, title=titulo)
    ss = getSampleStyleSheet()
    hh = ParagraphStyle('hh', parent=ss['Title'], textColor=colors.HexColor('#7A1517'), fontSize=16, spaceAfter=1, alignment=0)
    sb = ParagraphStyle('sb', parent=ss['Normal'], textColor=colors.HexColor('#666666'), fontSize=9, spaceAfter=10)
    el = [Paragraph("FROTA MACEDO ENGENHARIA", hh), Paragraph(titulo + (f" — {subtitulo}" if subtitulo else ""), sb)]
    t = Table([colunas] + linhas, repeatRows=1)
    stl = [('BACKGROUND',(0,0),(-1,0),colors.HexColor('#7A1517')),
           ('TEXTCOLOR',(0,0),(-1,0),colors.white),
           ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
           ('FONTSIZE',(0,0),(-1,-1),8.5),
           ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, colors.HexColor('#f6f2f2')]),
           ('GRID',(0,0),(-1,-1),0.4,colors.HexColor('#dddddd')),
           ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
           ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
           ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5)]
    for c, a in (aligns or {}).items():
        stl.append(('ALIGN',(c,1),(c,-1),a))
    t.setStyle(TableStyle(stl)); el.append(t)
    doc.build(el); return buf.getvalue()

@app.get("/notas/procurar")
def notas_procurar(request: Request, q: str = ""):
    """Panorama dos 4 marcos (PCO solicitado / conferida / entregue / protocolada) por O.C."""
    from fastapi import HTTPException
    exige(request, "PROCURAR_NOTA")
    q = (q or "").strip()
    if len(q) < 3: return {"itens": []}
    SEL = "id,numero,data_oc,centro_custo,fornecedor,valor,pco_enviado_em,qtd_notas,valor_notas,fisica_ok,protocolo_ok,ciclo_fechado"
    val = f"(numero.ilike.*{q}*,fornecedor.ilike.*{q}*,centro_custo.ilike.*{q}*)"
    url = (f"{SB_URL}/rest/v1/v_ocs?pco_status=eq.enviado&or={urllib.parse.quote(val)}"
           f"&select={SEL}&order=numero&limit=40")
    try: rows = _sb_json(url, SB_KEY) or []
    except Exception as e: raise HTTPException(500, f"procurar: {e}")
    # também busca pelo NÚMERO DA NOTA -> traz as O.C. dessas notas
    have = {r["id"] for r in rows}
    try:
        nrows = _sb_json(f"{SB_URL}/rest/v1/notas?numero_nota=ilike.{urllib.parse.quote('*'+q+'*')}&select=oc_id", SB_KEY) or []
        extra = [n["oc_id"] for n in nrows if n.get("oc_id") and n["oc_id"] not in have]
        if extra:
            idlist = ",".join(f'"{i}"' for i in dict.fromkeys(extra))
            e = _sb_json(f"{SB_URL}/rest/v1/v_ocs?id=in.({idlist})&pco_status=eq.enviado&select={SEL}", SB_KEY) or []
            rows.extend(e)
    except Exception: pass
    for r in rows:
        nurl = (f"{SB_URL}/rest/v1/notas?oc_id=eq.{urllib.parse.quote(r['id'])}"
                f"&select=numero_nota,emissao,valor,tem_boleto,recebimento,divergencia,entregue,vencimento,forma_pagamento&order=criado_em")
        try: r["notas"] = _sb_json(nurl, SB_KEY) or []
        except Exception: r["notas"] = []
        try: r["saldo"] = round(float(r.get("valor") or 0) - float(r.get("valor_notas") or 0), 2)
        except Exception: r["saldo"] = None
    return {"itens": rows}

_MOT_LABEL = {"sem_nota": "Sem nota (aguardando obra)", "a_receber": "A receber (via física)",
              "a_protocolar": "A protocolar", "divergencia": "Divergência"}

def _pendencias_calc(q="", cats=None):
    rows = _sb_json(f"{SB_URL}/rest/v1/v_ocs?pco_status=eq.enviado&ciclo_fechado=eq.false"
                    f"&select=id,numero,data_oc,centro_custo,fornecedor,valor,qtd_notas,valor_notas,fisica_ok,protocolo_ok&order=data_oc", SB_KEY) or []
    dv = _sb_json(f"{SB_URL}/rest/v1/notas?divergencia=not.is.null&select=oc_id", SB_KEY) or []
    dvset = {r["oc_id"] for r in dv}
    ql = (q or "").strip().lower(); out = []
    for r in rows:
        mot = []
        if (r.get("qtd_notas") or 0) == 0: mot.append("sem_nota")
        elif not r.get("fisica_ok"): mot.append("a_receber")
        elif not r.get("protocolo_ok"): mot.append("a_protocolar")
        if r["id"] in dvset: mot.append("divergencia")
        if not mot: continue
        if cats and not (set(mot) & set(cats)): continue
        if len(ql) >= 3:
            blob = " ".join(str(x or "") for x in (r.get("numero"), r.get("fornecedor"), r.get("centro_custo"))).lower()
            if ql not in blob: continue
        r["motivos"] = mot; out.append(r)
    return out

@app.get("/notas/pendencias")
def notas_pendencias(request: Request, q: str = ""):
    from fastapi import HTTPException
    exige(request, "PENDENCIAS_NOTA")
    try: return {"itens": _pendencias_calc(q)}
    except Exception as e: raise HTTPException(500, f"pendencias: {e}")

@app.get("/notas/pendencias_pdf")
def notas_pendencias_pdf(request: Request, q: str = "", cats: str = ""):
    from fastapi import HTTPException
    exige(request, "PENDENCIAS_NOTA")
    catl = [c for c in (cats or "").split(",") if c]
    itens = _pendencias_calc(q, catl or None)
    linhas = [[r.get("numero") or "", _data_br(r.get("data_oc")), (r.get("centro_custo") or "")[:28],
               (r.get("fornecedor") or "")[:30], _fmt_reais(r.get("valor")),
               ", ".join(_MOT_LABEL.get(m, m) for m in r.get("motivos", []))] for r in itens]
    pdf = _lista_pdf("Pendências de notas", ["O.C.", "Data", "Centro de custo", "Fornecedor", "Valor", "Pendência"],
                     linhas, subtitulo=f"{len(itens)} pendência(s)", aligns={4: 'RIGHT'})
    return Response(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="pendencias_notas.pdf"'})

def _vencimentos_calc(q="", filtro="todas"):
    rows = _sb_json(f"{SB_URL}/rest/v1/v_notas_app?vencimento=not.is.null"
                    f"&select=numero_nota,valor,vencimento,forma_pagamento,tem_boleto,oc_numero,fornecedor,centro_custo&order=vencimento", SB_KEY) or []
    hoje = datetime.date.today(); ql = (q or "").strip().lower(); out = []
    for r in rows:
        try: dias = (datetime.date.fromisoformat(str(r["vencimento"])[:10]) - hoje).days
        except Exception: dias = None
        r["dias"] = dias
        if filtro == "vencidas" and not (dias is not None and dias < 0): continue
        if filtro == "a_vencer" and not (dias is not None and dias >= 0): continue
        if len(ql) >= 3:
            blob = " ".join(str(x or "") for x in (r.get("oc_numero"), r.get("fornecedor"), r.get("centro_custo"), r.get("numero_nota"))).lower()
            if ql not in blob: continue
        out.append(r)
    return out

@app.get("/notas/vencimentos")
def notas_vencimentos(request: Request, q: str = "", filtro: str = "todas"):
    from fastapi import HTTPException
    exige(request, "VENCIMENTOS_NOTA")
    try: return {"itens": _vencimentos_calc(q, filtro)}
    except Exception as e: raise HTTPException(500, f"vencimentos: {e}")

@app.get("/notas/vencimentos_pdf")
def notas_vencimentos_pdf(request: Request, q: str = "", filtro: str = "todas"):
    from fastapi import HTTPException
    exige(request, "VENCIMENTOS_NOTA")
    itens = _vencimentos_calc(q, filtro)
    def sit(d):
        if d is None: return ""
        if d < 0: return f"vencida há {-d} d"
        if d == 0: return "vence hoje"
        return f"em {d} d"
    linhas = [[r.get("oc_numero") or "", (r.get("fornecedor") or "")[:28], r.get("numero_nota") or "",
               _fmt_reais(r.get("valor")), r.get("forma_pagamento") or "", _data_br(r.get("vencimento")), sit(r.get("dias"))]
              for r in itens]
    titf = {"vencidas": "vencidas", "a_vencer": "a vencer"}.get(filtro, "todas")
    pdf = _lista_pdf("Vencimentos de notas", ["O.C.", "Fornecedor", "NF", "Valor", "Pagamento", "Vencimento", "Situação"],
                     linhas, subtitulo=f"{len(itens)} nota(s) · {titf}", aligns={3: 'RIGHT'})
    return Response(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="vencimentos_notas.pdf"'})

# ================= NOTAS FISCAIS — PROTOCOLO (4º marco) =================
@app.get("/notas/protocolo_lista")
def notas_protocolo_lista(request: Request, q: str = ""):
    """O.C. totalmente entregues (via física recebida) e ainda NÃO protocoladas."""
    from fastapi import HTTPException
    exige(request, "GERAR_PROTOCOLO")
    url = (f"{SB_URL}/rest/v1/v_ocs?pco_status=eq.enviado&fisica_ok=eq.true&protocolo_ok=eq.false&qtd_notas=gt.0"
           f"&select=id,numero,data_oc,centro_custo,fornecedor,valor,qtd_notas,valor_notas&order=centro_custo,numero")
    try: rows = _sb_json(url, SB_KEY) or []
    except Exception as e: raise HTTPException(500, f"listar: {e}")
    ql = (q or "").strip().lower()
    if len(ql) >= 3:
        rows = [r for r in rows if ql in " ".join(str(x or "") for x in (r.get("numero"), r.get("fornecedor"), r.get("centro_custo"))).lower()]
    return {"itens": rows, "total": len(rows)}

@app.post("/notas/protocolar")
async def notas_protocolar(request: Request):
    """Marca a O.C. como protocolada (4º marco): cria o protocolo e fecha o ciclo."""
    from fastapi import HTTPException
    u, p = exige(request, "GERAR_PROTOCOLO")
    b = await request.json()
    oc_id = str(b.get("oc_id", "")).strip()
    numero_prot = (b.get("numero") or "").strip() or None
    if not oc_id: raise HTTPException(400, "oc_id?")
    oc = _sb_json(f"{SB_URL}/rest/v1/ocs?id=eq.{urllib.parse.quote(oc_id)}&select=numero,centro_custo&limit=1", SB_KEY) or []
    if not oc: raise HTTPException(404, "O.C. não encontrada")
    prot = {"data": datetime.date.today().isoformat(), "centro_custo": oc[0].get("centro_custo"),
            "numero": numero_prot or oc[0].get("numero"), "criado_por": u["id"]}
    req = urllib.request.Request(f"{SB_URL}/rest/v1/protocolos",
        data=json.dumps(prot, ensure_ascii=False).encode(), method="POST",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "content-type": "application/json",
                 "prefer": "return=representation"})
    try: pid = (json.loads(urllib.request.urlopen(req, timeout=30).read().decode()) or [{}])[0].get("id")
    except urllib.error.HTTPError as e: raise HTTPException(500, "criar protocolo: " + e.read().decode()[:200])
    patch = {"protocolo": True, "protocolado_em": datetime.datetime.now().isoformat(), "protocolo_id": pid}
    req2 = urllib.request.Request(f"{SB_URL}/rest/v1/ocs?id=eq.{urllib.parse.quote(oc_id)}",
        data=json.dumps(patch, ensure_ascii=False).encode(), method="PATCH",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "content-type": "application/json",
                 "prefer": "return=minimal"})
    try: urllib.request.urlopen(req2, timeout=30)
    except urllib.error.HTTPError as e: raise HTTPException(500, "marcar protocolo: " + e.read().decode()[:200])
    log_frotahub(u["id"], p["papel"], "GERAR_PROTOCOLO", "PROTOCOLOU", f"{oc[0].get('numero')}")
    return {"ok": True}

# ---------------- ENVIAR_PCO ----------------
def _fmt_reais(v):
    try: return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception: return "—"

def _pco_email_html(aprovadas, data_str):
    th = "style='background:#000;color:#fff;padding:7px 9px;text-align:left;font-size:13px'"
    td = "style='padding:7px 9px;border-bottom:1px solid #ddd;font-size:13px'"
    linhas = ""; total = 0.0
    for o in aprovadas:
        try: total += float(o.get("valor") or 0)
        except Exception: pass
        linhas += (f"<tr><td {td}>{o.get('centro_custo') or '—'}</td>"
                   f"<td {td}>{o.get('numero') or '—'}</td>"
                   f"<td {td}>{o.get('fornecedor') or '—'}</td>"
                   f"<td {td}>{o.get('cnpj_fornecedor') or '—'}</td>"
                   f"<td {td} align='right'>{_fmt_reais(o.get('valor'))}</td></tr>")
    total_row = (f"<tr><td {td} colspan='4'><b>Total ({len(aprovadas)} ordens)</b></td>"
                 f"<td {td} align='right'><b>{_fmt_reais(total)}</b></td></tr>")
    return f"""<div style="font-family:Arial,sans-serif;color:#1e2733;font-size:14px">
<p>Prezados, equipe Distribuidora de Alimentos Fartura S/A,</p>
<p style="margin-bottom:18px">Seguem as Ordens de Compra (PCO) para solicitação. Os PDFs correspondentes
seguem no arquivo em anexo.</p>
<table style="border-collapse:collapse;width:100%;max-width:720px">
<thead><tr><th {th}>CENTRO DE CUSTO</th><th {th}>OC</th><th {th}>FORNECEDOR</th><th {th}>CNPJ</th><th {th}>VALOR (R$)</th></tr></thead>
<tbody>{linhas}{total_row}</tbody></table>
<p style="margin-top:16px">Arquivo em anexo: <b>Pedidos_PCO_{data_str.replace('/','-')}.zip</b></p>
<p style="margin-top:24px">Atenciosamente,</p>
<p>{ASSINATURA}</p></div>"""

def _bloq_email_html(bloqueadas, data_str):
    itens = "".join(f"<li>O.C. <b>{o.get('numero') or o.get('arquivo')}</b> — {o.get('motivo') or 'bloqueada'}</li>" for o in bloqueadas)
    return f"""<div style="font-family:Arial,sans-serif;color:#1e2733;font-size:14px">
<p>As seguintes Ordens de Compra <b>NÃO</b> foram enviadas no PCO de {data_str} (precisam de correção):</p>
<ul>{itens}</ul>
<p style="margin-top:20px">Atenciosamente,</p><p>{ASSINATURA}</p></div>"""

def _enviar_brevo(assunto, html, para, cc, anexos):
    import base64
    payload = {"sender": {"email": PCO_FROM, "name": "Frota Macedo Engenharia"},
               "to": [{"email": e} for e in para], "subject": assunto, "htmlContent": html}
    if cc: payload["cc"] = [{"email": e} for e in cc]
    if anexos:
        payload["attachment"] = [{"name": n, "content": base64.b64encode(c).decode()} for n, c, _ in anexos]
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode(),
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json", "accept": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo {e.code}: {e.read().decode()[:300]}") from None

def enviar_email(assunto, html, para, cc=None, anexos=None):
    # Preferir API HTTP (Brevo) — o Render bloqueia SMTP.
    if BREVO_API_KEY:
        return _enviar_brevo(assunto, html, para, cc, anexos)
    import smtplib, ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = assunto; msg["From"] = PCO_FROM
    msg["To"] = ", ".join(para)
    if cc: msg["Cc"] = ", ".join(cc)
    msg.set_content("Seu cliente de e-mail não suporta HTML.")
    msg.add_alternative(html, subtype="html")
    for nome, conteudo, mime in (anexos or []):
        mt, st = mime.split("/", 1)
        msg.add_attachment(conteudo, maintype=mt, subtype=st, filename=nome)
    destinos = list(para) + list(cc or [])
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
            s.login(SMTP_USER, SMTP_PASS); s.send_message(msg, to_addrs=destinos)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
            s.ehlo(); s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg, to_addrs=destinos)

def _oc_row(d, status, path, motivo=None):
    row = {"numero": d.get("numero"), "data_oc": d.get("data_oc"),
           "centro_custo": d.get("centro_custo"), "cnpj_centro_custo": d.get("cnpj_centro_custo"),
           "fornecedor": d.get("fornecedor"), "cnpj_fornecedor": d.get("cnpj_fornecedor"),
           "valor": d.get("valor"), "pco_status": status, "arquivo_oc_path": path}
    if status == "enviado": row["pco_enviado_em"] = datetime.datetime.now().isoformat()
    if motivo: row["bloqueio_motivo"] = motivo
    return row

def sb_upsert_ocs(rows):
    rows = [r for r in rows if r.get("numero")]
    if not rows: return
    req = urllib.request.Request(f"{SB_URL}/rest/v1/ocs?on_conflict=numero",
        data=json.dumps(rows, ensure_ascii=False).encode(), method="POST",
        headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "content-type": "application/json",
                 "prefer": "resolution=merge-duplicates,return=minimal"})
    try: urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e: print("ocs upsert erro:", e.read().decode()[:300], flush=True)

def _oc_corrigida(numero):
    """Se existe registro de O.C. corrigida (pendente + corrigida=true), devolve os campos
    corretos do banco — usados no lugar do que foi relido do PDF (que ainda traz o erro)."""
    try:
        q = urllib.parse.urlencode({"numero": f"eq.{numero}", "pco_status": "eq.pendente",
            "corrigida": "eq.true", "limit": "1",
            "select": "centro_custo,cnpj_centro_custo,fornecedor,cnpj_fornecedor,valor,data_oc,endereco_fornecedor"})
        r = _sb_json(f"{SB_URL}/rest/v1/ocs?{q}", SB_KEY) or []
        return r[0] if r else None
    except Exception: return None

def _pco_coleta(access):
    """Baixa os PDFs de 0-ENVIAR, extrai e separa aprovadas/bloqueadas (ignora já enviadas)."""
    arqs = [a for a in dropbox_rateio.listar(access, PCO_ENVIAR) if a.lower().endswith(".pdf")]
    try:
        env = _sb_json(f"{SB_URL}/rest/v1/ocs?pco_status=eq.enviado&select=numero", SB_KEY) or []
        ja = {str(r["numero"]) for r in env}
    except Exception: ja = set()
    aprov, bloq = [], []
    for nome in sorted(arqs):
        pdf = dropbox_rateio.baixar(access, f"{PCO_ENVIAR}/{nome}")
        if not pdf: continue
        d = extrai_oc(_oc_pdf_texto(pdf)); d["arquivo"] = nome; d["_pdf"] = pdf
        num = d.get("numero")
        if num and num in ja: continue
        corr = _oc_corrigida(num) if num else None
        if corr:                       # foi corrigida na OC_INVALIDA -> usa os dados do banco
            for k in ("centro_custo","cnpj_centro_custo","fornecedor","cnpj_fornecedor","valor","data_oc"):
                if corr.get(k) not in (None, ""): d[k] = corr[k]
            d["valido"] = True; d["motivo"] = ""
        (aprov if d.get("valido") else bloq).append(d)
    return aprov, bloq

@app.post("/pco/enviar_previa")
def pco_enviar_previa(request: Request):
    from fastapi import HTTPException
    exige(request, "ENVIAR_PCO")
    if not dropbox_rateio.ativo(): raise HTTPException(500, "Dropbox não configurado")
    access = dropbox_rateio.obter_token()
    aprov, bloq = _pco_coleta(access)
    limpo = lambda d: {k: d.get(k) for k in ("arquivo","numero","data_oc","centro_custo","fornecedor","cnpj_fornecedor","valor","motivo")}
    def limpo_bl(d):
        r = {k: d.get(k) for k in ("arquivo","numero","data_oc","centro_custo","cnpj_centro_custo","fornecedor","cnpj_fornecedor","valor","motivo")}
        cc = re.sub(r"\D", "", r.get("cnpj_centro_custo") or "")
        if not cc.startswith(FART_BASE): r["cnpj_cc_sugerido"] = _sugerir_cnpj_cc(r.get("centro_custo"))
        return r
    return {"aprovadas": [limpo(d) for d in aprov], "bloqueadas": [limpo_bl(d) for d in bloq],
            "destinatarios": {"to": PCO_TO, "cc": PCO_CC, "bloqueadas": PCO_BLOQ_TO},
            "smtp_ok": bool(BREVO_API_KEY or (SMTP_USER and SMTP_PASS))}

@app.post("/pco/enviar")
def pco_enviar(request: Request):
    import io, zipfile
    from fastapi import HTTPException
    u, p = exige(request, "ENVIAR_PCO")
    if not (BREVO_API_KEY or (SMTP_USER and SMTP_PASS)):
        raise HTTPException(500, "Envio não configurado — defina BREVO_API_KEY (recomendado) no Render.")
    access = dropbox_rateio.obter_token()
    hoje = datetime.date.today().strftime("%d/%m/%Y")
    aprov, bloq = _pco_coleta(access)
    if not aprov and not bloq:
        return {"ok": True, "enviados": 0, "bloqueados": 0, "msg": "Nada novo para enviar."}
    erros = []
    # ---- APROVADAS: e-mail com zip + move + banco ----
    if aprov:
        buf = io.BytesIO(); z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
        for d in aprov: z.writestr(d["arquivo"], d["_pdf"])
        z.close(); zipbytes = buf.getvalue()
        # nome do zip com data+hora -> nunca sobrescreve um envio anterior do mesmo dia
        zipnome = f"Pedidos_PCO_{datetime.datetime.now().strftime('%d-%m-%Y_%H%M%S')}.zip"
        try:
            enviar_email(f"Ordens de Compra (PCO) - {hoje} - Frota Macedo Engenharia",
                         _pco_email_html(aprov, hoje), PCO_TO, PCO_CC,
                         [(zipnome, zipbytes, "application/zip")])
        except Exception as e:
            raise HTTPException(500, f"falha ao enviar e-mail: {e}")
        # SÓ o ZIP fica arquivado em "1 - PEDIDOS FEITOS" (sem subpasta APAGAR).
        feitos = f"{PCO_BASE}/1 - PEDIDOS FEITOS"
        dropbox_rateio.criar_pasta(access, feitos)
        zip_rel = f"1 - PEDIDOS FEITOS/{zipnome}"
        zip_ok = True
        try: dropbox_rateio.subir_bytes(access, zipbytes, f"{feitos}/{zipnome}", overwrite=False)
        except Exception as e: erros.append(f"zip: {e}"); zip_ok = False
        rows = []
        for d in aprov:
            if zip_ok:
                # a via solta sai da pasta 0-ENVIAR (fica preservada dentro do ZIP);
                # vai para a lixeira do Dropbox (recuperável ~30 dias)
                try: dropbox_rateio.apagar(access, f"{PCO_ENVIAR}/{d['arquivo']}")
                except Exception as e: erros.append(f"remover {d['arquivo']}: {e}")
                path = f"{zip_rel}|{d['arquivo']}"        # <zip>|<arquivo interno>
            else:
                path = f"0 - ENVIAR (ADICIONAR AQUI)/{d['arquivo']}"
            rows.append(_oc_row(d, "enviado", path))
        sb_upsert_ocs(rows)
    # ---- BLOQUEADAS: move + banco + aviso ----
    if bloq:
        pasta_bloq = f"{PCO_BASE}/2 - OCS BLOQUEADAS"; dropbox_rateio.criar_pasta(access, pasta_bloq)
        rows = []
        for d in bloq:
            novo = f"OC_BLOC_{d.get('numero') or os.path.splitext(d['arquivo'])[0]}.pdf"
            try: dropbox_rateio.mover(access, f"{PCO_ENVIAR}/{d['arquivo']}", f"{pasta_bloq}/{novo}")
            except Exception as e: erros.append(f"mover bloq {d['arquivo']}: {e}")
            rows.append(_oc_row(d, "bloqueado", f"2 - OCS BLOQUEADAS/{novo}", motivo=d.get("motivo")))
        sb_upsert_ocs(rows)
        try: enviar_email(f"O.C. bloqueadas no PCO - {hoje}", _bloq_email_html(bloq, hoje), PCO_BLOQ_TO)
        except Exception as e: erros.append(f"e-mail bloqueadas: {e}")
    log_frotahub(u["id"], p["papel"], "ENVIAR_PCO", "ENVIOU_PCO",
                 f"{len(aprov)} enviadas / {len(bloq)} bloqueadas", {"to": PCO_TO})
    return {"ok": True, "enviados": len(aprov), "bloqueados": len(bloq),
            "to": PCO_TO, "cc": PCO_CC, "erros": erros}

# ---------------- PCOS_ENVIADOS (planilha do banco) ----------------
def _pco_enviados_query(desde, ate):
    parts = ["pco_status=eq.enviado",
             "select=numero,data_oc,centro_custo,fornecedor,cnpj_fornecedor,valor,pco_enviado_em",
             "order=pco_enviado_em.desc"]
    if desde: parts.append(f"pco_enviado_em=gte.{desde}")
    if ate:   parts.append(f"pco_enviado_em=lte.{ate}T23:59:59")
    try: return _sb_json(f"{SB_URL}/rest/v1/ocs?" + "&".join(parts), SB_KEY) or []
    except Exception: return []

@app.get("/pco/enviados")
def pco_enviados(request: Request, desde: str = "", ate: str = ""):
    exige(request, "PCOS_ENVIADOS")
    itens = _pco_enviados_query(desde, ate)
    total = sum((x.get("valor") or 0) for x in itens)
    return {"itens": itens, "total": total}

@app.get("/pco/enviados_xlsx")
def pco_enviados_xlsx(request: Request, desde: str = "", ate: str = ""):
    exige(request, "PCOS_ENVIADOS")
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    itens = _pco_enviados_query(desde, ate)
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "PCOs enviados"
    heads = ["O.C.", "Data O.C.", "Centro de custo", "Fornecedor", "CNPJ", "Valor (R$)", "Enviado em"]
    ws.append(heads)
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="A11F22"); c.alignment = Alignment(horizontal="center")
    def br(s): return "/".join(reversed(str(s)[:10].split("-"))) if s else ""
    for it in itens:
        ws.append([it.get("numero"), br(it.get("data_oc")), it.get("centro_custo"),
                   it.get("fornecedor"), it.get("cnpj_fornecedor"), it.get("valor"), br(it.get("pco_enviado_em"))])
    for i, w in enumerate([12, 12, 28, 34, 20, 14, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO(); wb.save(buf)
    return Response(content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="PCOs_enviados.xlsx"'})

# ================= FERRAMENTAS DO BUILDER (Config) =================
# Migração antigo -> novo (1:1) e Desfazer por data. SÓ builder.
OLD_BASES = {
    "ADM":   ("/AUTOMACAO ADMINISTRATIVO", "/FROTAHUB/1 - ADMINISTRATIVO"),
    "MANUT": ("/AUTOMACAO MANUTENCAO",     "/FROTAHUB/2 - MANUTENCAO"),
}
_MIG_JUNK = ("ARQUIVOS RESIDUAIS", "SKILLS", "APP (", "APP(", "_RODAR LOCAL")
def _mig_pular(nome):
    u = (nome or "").upper()
    return nome.startswith("_") or any(j in u for j in _MIG_JUNK)

def _exige_builder(request):
    from fastapi import HTTPException
    u, p = exige(request, "CONFIG_BUILDER")   # sem linha em permissoes -> só builder passa
    if p.get("papel") != "builder":
        raise HTTPException(403, "apenas builder")
    return u, p

@app.post("/migrar/plano")
async def migrar_plano(request: Request):
    from fastapi import HTTPException
    _exige_builder(request)
    dep = (await request.json()).get("depto")
    if dep not in OLD_BASES: raise HTTPException(400, "depto inválido")
    de_base, para_base = OLD_BASES[dep]
    access = dropbox_rateio.obter_token()
    itens = []
    for e in dropbox_rateio.listar_entradas(access, de_base):
        if _mig_pular(e["name"]): continue
        if e["dir"] and e["name"].upper() == "ORCAMENTOS MONTADOS":
            for m in dropbox_rateio.listar_entradas(access, f"{de_base}/{e['name']}"):
                if _mig_pular(m["name"]): continue
                itens.append({"nome": f"{e['name']}/{m['name']}",
                              "de": f"{de_base}/{e['name']}/{m['name']}",
                              "para": f"{para_base}/{e['name']}/{m['name']}"})
        else:
            itens.append({"nome": e["name"], "de": f"{de_base}/{e['name']}", "para": f"{para_base}/{e['name']}"})
    return {"depto": dep, "de_base": de_base, "para_base": para_base, "itens": itens}

@app.post("/migrar/limpar")
async def migrar_limpar(request: Request):
    from fastapi import HTTPException
    u, p = _exige_builder(request)
    dep = (await request.json()).get("depto")
    if dep not in OLD_BASES: raise HTTPException(400, "depto inválido")
    _, para_base = OLD_BASES[dep]
    access = dropbox_rateio.obter_token()
    apagados = 0
    for e in dropbox_rateio.listar_entradas(access, para_base):
        try: dropbox_rateio.apagar(access, f"{para_base}/{e['name']}"); apagados += 1
        except Exception: pass
    log_frotahub(u["id"], p["papel"], "CONFIG_BUILDER", "MIGRAR_LIMPAR", dep, {"apagados": apagados})
    return {"ok": True, "apagados": apagados}

@app.post("/migrar/copiar")
async def migrar_copiar(request: Request):
    from fastapi import HTTPException
    _exige_builder(request)
    b = await request.json(); de = b.get("de"); para = b.get("para")
    if not de or not para: raise HTTPException(400, "de/para?")
    access = dropbox_rateio.obter_token()
    dropbox_rateio.criar_pasta(access, para.rsplit("/", 1)[0])
    try:
        dropbox_rateio.copiar(access, de, para)
    except Exception as e:
        return {"ok": False, "erro": str(e)[:200]}
    return {"ok": True}

@app.post("/desfazer")
async def desfazer(request: Request):
    from fastapi import HTTPException
    u, p = _exige_builder(request)
    b = await request.json(); dep = b.get("depto"); desde = (b.get("desde") or "").strip()
    if not desde: raise HTTPException(400, "informe a data")
    access = dropbox_rateio.obter_token()
    if dep != "ADM":
        return {"ok": False, "depto": dep,
                "msg": "Desfazer da Manutenção virá com o journal. Para resetar a Manutenção nos testes, use a Migração (recopiar do antigo)."}
    # ADM/PCO: reverte ocs criadas a partir da data -> volta o arquivo p/ 0-ENVIAR e apaga a linha
    q = urllib.parse.urlencode({"criado_em": f"gte.{desde}", "select": "numero,arquivo_oc_path", "order": "criado_em.desc"})
    try: rows = _sb_json(f"{SB_URL}/rest/v1/ocs?{q}", SB_KEY) or []
    except Exception as e: raise HTTPException(500, f"ler ocs: {e}")
    import io, zipfile
    voltas, erros, zip_cache = 0, [], {}
    dropbox_rateio.criar_pasta(access, PCO_ENVIAR)
    for r in rows:
        path = r.get("arquivo_oc_path")
        if not path: continue
        try:
            if "|" in path:                         # enviada: extrai a via de dentro do ZIP
                zrel, inner = path.split("|", 1)
                zbytes = zip_cache.get(zrel)
                if zbytes is None:
                    zbytes = dropbox_rateio.baixar(access, f"{PCO_BASE}/{zrel}")
                    zip_cache[zrel] = zbytes
                if not zbytes: erros.append(f"{r.get('numero')}: ZIP não encontrado"); continue
                with zipfile.ZipFile(io.BytesIO(zbytes)) as zf: pdf = zf.read(inner)
                dropbox_rateio.subir_bytes(access, pdf, f"{PCO_ENVIAR}/{inner}", overwrite=True)
                voltas += 1
            else:                                   # bloqueada/solta: apenas move de volta
                nome = path.rsplit("/", 1)[-1]
                dropbox_rateio.mover(access, f"{PCO_BASE}/{path}", f"{PCO_ENVIAR}/{nome}")
                voltas += 1
        except Exception as e: erros.append(f"{r.get('numero')}: {str(e)[:70]}")
    nums = [r["numero"] for r in rows if r.get("numero")]
    if nums:
        inlist = ",".join(f'"{n}"' for n in nums)
        req = urllib.request.Request(f'{SB_URL}/rest/v1/ocs?numero=in.({inlist})', method="DELETE",
            headers={"apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "prefer": "return=minimal"})
        try: urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e: erros.append("apagar ocs: " + e.read().decode()[:150])
    log_frotahub(u["id"], p["papel"], "CONFIG_BUILDER", "DESFAZER_ADM", desde, {"ocs": len(rows)})
    return {"ok": True, "depto": "ADM", "ocs_revertidas": len(rows), "arquivos_voltaram": voltas, "erros": erros}

# ================= MANUTENÇÃO — CONFERIR_LISTA_ORCAMENTOS / GERAR_ORCAMENTOS =================
import base64 as _b64
from copy import deepcopy as _dcopy
from reportlab.lib.pagesizes import A4 as _A4
from reportlab.lib.units import mm as _mm
from reportlab.lib import colors as _col
from reportlab.lib.styles import ParagraphStyle as _PS
from reportlab.platypus import (SimpleDocTemplate as _SDT, Table as _T, TableStyle as _TS,
                                Paragraph as _P, Spacer as _SP, Image as _IMG)
from reportlab.lib.enums import TA_RIGHT as _TR, TA_CENTER as _TC

MANUT_BASE   = os.environ.get("MANUT_BASE", "/FROTAHUB/2 - MANUTENCAO").rstrip("/")
ORC_NOTAS    = MANUT_BASE + "/0 - NOTAS PARA ORCAMENTO (COLOCAR AQUI)"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
ORC_EXTRAPOLA  = float(os.environ.get("ORC_EXTRAPOLA", "500"))   # nota (antes do +20%) acima disso -> extrapolado
_LOGO_ORC_B64  = "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"
_MODELO_DOCX_B64 = "UEsDBAoAAAAAAC1g/VwAAAAAAAAAAAAAAAAFAAAAd29yZC9QSwMECgAAAAAALWD9XAAAAAAAAAAAAAAAAAsAAAB3b3JkL19yZWxzL1BLAwQKAAAACAAtYP1cgwyTCjUBAABHBQAAHAAAAHdvcmQvX3JlbHMvZG9jdW1lbnQueG1sLnJlbHOtlF9vgyAUxb+K4X2CTqsutX1ZlvR1cR8A4aJuAgbosn77sax/7NKZPvB4D3DOLyc3rLdfcow+wdhBqxolMUERKKb5oLoavTUvDyXabtavMFLnb9h+mGzknyhbo9656Qljy3qQ1MZ6AuVPhDaSOj+aDk+UfdAOcErICpu5B7r2jHa8RmbHExQ1hwnu8dZCDAyeNdtLUO5GBLbuMIL1jtR04Gr0O8feB+Hb8WnIeLWXLRjf44XgLC1BPIaEEFo7pd28hrO0BJGFhADF/zCclCWEPOgugHO+9/k2HJUlhFVIBKblz9EM4aQsIRShtwHM9SqASZbyy5D5g/S/wSVeAh8oJqQogFesTMqSizbNsqLgOW+rFamEyFie5UAKkcTvU/cfZRW2JeUa2o4wL+oonarCVz/i5htQSwMECgAAAAgALWD9XIoRD9eNDAAAhrQAABEAAAB3b3JkL2RvY3VtZW50LnhtbO1d23LiOBp+FRW7FzNVSYwNGEhNZtYBk85WgCyQ7N4qtgBt25ZHNiTpq6nai3mB3QfomYt+h73cvMk8yUo+cXI4J+GgVDfItvRL+v/vP+i3sX746cm2wBBRDxPnIiOfZTMAOQYxsdO7yNx1aqelDPB86JjQIg66yDwjL/PTjz88npvEGNjI8YFtnF/3HELhg8WuP8p58CgXwKMr5zOAEXe880fXuMj0fd89lyTP6CMbemc2NijxSNc/M4gtkW4XG0h6JNSUlKycDUouJQbyPDaSCnSG0IvJ2bPUiIscdrFLqA19dkh7kg3p54F7yqi70McP2ML+M6OdVWMy5CIzoM55ROI0GRBvch4OKPqKW9Bl+g2bVCPuBD1KFFlsDMTx+tgdTWNdauxiPyYynDeJoW2NRCDnN5NBlcJH9jUiuMzwzbCRbYUjn09Rzi4hEU4iabHMECb7jEdiQ+yMOl6LNWPMlQurEVCmCbi9zYRzRcnAHVHDm1G7dj4ntLjOr0ArEvL41LzNBtPuQzfRQONpOWIR7ji9vGT0IfXR04iGvDKRglSWSrOElDUIsQkq8iyp3MqkVImPaobQklieIsRGNUNpSVBPU0qZnLoeJWWWUnE9SrlZSqX1KM3AiRmSz2uQwiMdg3bOXJlCUbKJiazcyBjKqoGWVI9Y10qRskrGaD6cDl5yPDEdNaGDx8ez3mDGCHimb/ZXoqLEtlnibaEP+9Drj1NczZwxfY3JPduMRzzweSDmM//2H6zo65ZGhb8D9vXssj7MJ5hhB8xDlYvZbEaKKlwyYizWCo6IyyoMoXWRcVhkxWsbxCIsxqgFf/yE9+UiEzW2UNdfpf4D8X1ir9KC4l5/pS6w42ETfVq9yf3yTaRJtkmT/L6i2OTFHvuuECtkuFyIGT5xulBOPa3ksmMdxQT9sAcj/Iz6Mw5IetLUfIaahXtO3N5gsSaiSb1w/m7wEZY9FxrMELH6sMtqcqKs/IBY4IVGPUSVg4/IdLGiy1BgYQcBE3t+J2jKS5dJ6SYptXgpaIKefL7W4P5flvNMZqyG8cykWkzEzSp1u8jw9bCqH1AJh/YQfFoxs1y+eLmlgJs5OQMcaLNBsx6RZ1Be8LHPlzJRXaMxvKLQ7WOjRllNPifIMDQ6c0OMz15sy9eIisNY1CGVPnR6SPNcNgk+sICJ8/vftNcxUlVmLMGAzoaOi0m52PAHFDFqrHTuJsNipY2pOcNbHACQHzBWRGLLzopNGtUJW0A+gFA4s8wdnaKUPPYRNL2Y55NUpJlRPFjYrWHL4j3wMqDnyH5AbFT02mTLZYOtl30UaaHEa3nUaLF+w7JPkW/0ebHLiETnpbEL0mQn/MhjqgQeHuvM5V9k4MAngeSeutTm38xngaeAJ88RxCFXmHnaIo1au9TzrxCzNrzA5sAGFFCHwxsvGlpcJRqb5yaMYf+DGmMgGj8OERzqe2ASEjMgBXZBCsyKNDK1wuBuy+DSqFwjju/xdp6BmXJXoIUfKA4G600cIuj5mofhxMm+xjz26EzIq/Cz4gXfwZTjeci1XEnNh9W8L/FZJTlT8SbPSck4fW4zgmmy+bgUeYgOmQ2otZodDdS1il5tAr1xpTc+aa1rDdx0qlrAq0kk7S7TJthU0NR8UZlmk1yYZVN4biGbdKd3Bj4h7LM+TASaFh4iTCHQrIfBzwNE2f8ToJQL4I9f/g0q2ISskkk8UBs4BibOy1eK2RG/WCPUhxb6AqWKLhicMPi7UuF7oMgl+VRmCAeMVb8B0KXEhzaraRLEPInDFoYY/qXHPKvF1xJRrUrj9q9AKZ7l1NyZouQkZo3l02I2hbnCDm7DDj6e/9OI6QWD2zkYqikwVJeCoVKWskW+zFW3oZuHzKkmffkGec6cAOd//wWyIucLc3ROCtd8UrKsH2ele2nSWeXgyUlrHO2R84vQLifmL2o9IxBZnRCJOmEvY5GKLMNuZhlihq+fN1iAH2VWIsu1WAmlSopUlmkybca9vsku8wXNePWofbBgSUy8hSCNoGrUIR1xZRrTsjKJypnr6jQKX6UQz+8VElIylv0OvyNoT1nRXHbWiuayy1nR1suvWl1vdJqgMc+K7i63JvhTVasVXd2il2np99ftl381gbyma5l1CsoHMmuMJ6UUnpSW4kkqPIQbOxo3ppf1Sk1exY2ltljgxlLbzHdjKU3mubFR9c3cWHlTL1Y+LieWnkPawCA1L1vaOQATdul9/c7Wp1TXGncdvVHRmqB619DaAPxpoxXOorWJcEPCDb2lG8oXU2/O5lLP5jf2WXspvElfNWbjlwR1fjGoZ1xmpVypVbVVfHlqiwW+PLXNfF+e2mQK3DONYgfOm+RTwb24yQbgFqmCN0sVFBbEWMqiGKuwKMZSDirGSk8UyMUUb1tcyttWtWqzDapNcNvS2x120FoYiWyoAvtkmqZVYAGaS5uiufRWaM5/JIK3AtQGsdH5O68FtjLwGr/ZCOrB3UagJ7cb17wTvv+C5LdV91KQS90KPnjx1fgDYPxZBRf2wvt0eynMS2Ihn4BcFpgYescoyOC5QSbHLvQHdI8lOffW+qs5FCm5dpxrz12Nfd8qhXTUwhaJBpFoWCgXkWiYuzQTiYYtJRo6zbpIM4g0w45GxXubZqgjakATO33igfbL7wTcDPCX4Mnw6sA5zhXO3mYasrmzopI9K5UUnmlQTnPlYxSf7jB7iF6+7eeyVBueRdk+hCnhP+hA1CdMNb+eAJn9gVNwC11sDI5RtOEvWSTd86G5n+JNfm7D5Jj6g5slMg+rPdmhZA9wWb7oaZaPnPOc6PiV54LKKawpLxcdX7crrev6dUN7+ZU/lspj5euO3min8mx7T8MsswKeGzSn1F8QMqe0mB8wv7oo37l1fPrDMHk59bScfjr9bQf8EbE5p1MTBzQZ7CfErC2N6sWn11tWVQtVTVdWWValtliAkdQ281GS0mQX8grZjfMK2am12OMb//Rugt6HW9qt5yGYVa1vnh8WenEUenGQGlDVeawRxBlCEYQiCAcxqR5/u9ManTOhGUIzhGZMasZd47oqFEMoxuqK8f4vqXhPvbjXbpotwLRDuA2hHUI7prSj0+xoN299t3+fleTo8P4RYVLcdxdaHsrE8E85uxXMz3mrhID4oUL8UMHcubtsAr19BW7vKyCfrddB5/qqNeeep4D4oUL8yKz4SXYLb3sUKBco32WUi7thRw7xD1h+vi/CW38G5ZN8SeBc4Py4cS5yLeMJyVquVqiVRELyGIMeRTgDoRdHndW5vKt80pK0TiH7jyCzw49vrkSCR6iIcB2vuQ6RFRKqIVQj/QEhoRhCMcTCPGVhnj9RRQJKKIdQjvSsVW7xy7uOOGt1dKA//FApJ5zB8UH8UMF8c3cfppLa1/XbG70NCslTQpJW1xoV8UOzI0T7cRl0kRYSKD98lIsMz7Fj/BjWo7kTVdhygfODx3nxRFm88Vx63oW/hqjtwoT9+Tg9dwS6MJ64rFZ0Wdc2T1yqmyqQuqoC7a2axIoxrQrrv5gu+IkiuNJb836oeJRmfw2o70F6ZEc2GTh2pAij+PZGcf1XUrL4QFFPsvmFAQL/WuL9paUDVPglXtm6EyjY4N0WkL8bwCc+tIDLSujJR45H3n1bZhx+bnNqQ8xWDAABD2EPUATZJwI/D6BPCeCrCTgkXtoLPR7f8v2r+7Rtwc6+h7Us9lP56LewiP1U3vi1Ks3Ltt6656+L/o+e9pborT48sU9WacXM1aLNVNRFQF64mYq6JpDzuxEwrQ/RP3757b3DhO2oFn35Fm6ICIYvXy1skiD0KYLvPOSj74MdI4FBKPMrxAMQuJD6mAITsn/hnorIxp738jtJDx+E1Hdx3E0PDHmwi5hIDWxDgB3DGiAb2JAJBrP4l4eKdIhfvpFAxo5PUQ/OeeXbauujwmHu77C9WHkv92SciZGXb3K/fJM1YuR8+mYFufQtDLYfUJe0ck7LxrNQd1R8KwYT67r6nXusYCJ2LWhqvqjMGAo1xVCoSxmKFbdEXzGtupd2YhJoe7DW2dUcurArx2tXNANhHwFmVCoW5pML9j/kWzoCCfD9zleI1Txk+GFnXUIYm1qoiyhyDDQSD+rCgeVnAD3H5kWGXptR8On22l9AtMdPOasGvOBzKOWCX7gQysfGxk2oTyH240ZMsiAAAJN3uAoIQMUgXQjlGIAyucgxnVzrBzv88B2Igi7CISeHvYEfwSHqqjGwO2wOwZFJDO7QOUXsoFvsG2ywuQRxMR8kPgDzOSiwJgO+Pvrx/1BLAwQKAAAACAAtYP1cmNPGwg0DAAATEQAADwAAAHdvcmQvc3R5bGVzLnhtbOVWW0/bMBT+K1HeIU2aFqgoiBUqkKYNcdGeXcdpLBw7sx1K+fWzEzu9pKGFBiZtT825+PP5zqU+p+cvKXGeEReY0aHrH3ZcB1HIIkynQ/fxYXxw7DpCAhoBwigaunMk3POz09lAyDlBwknh4GZKGQcToqwzP3Rmfs91FCoVgxQO3UTKbOB5AiYoBeKQZYgqY8x4CqQS+dRLAX/KswPI0gxIPMEEy7kXdDp9C8N3QWFxjCG6ZDBPEZXFeY8johAZFQnOhEWb7YI2YzzKOINICJWJlJR4KcC0gvHDGlCKIWeCxfJQkTERFVDquN8pvlKyAOi9DyCwADr9EYOXKAY5kUKL/JYb0UjFz5hRKZzZAAiI8dAdAYInHLtKA8WKiICQFwKDFWVyQcXSKe/s1DPQ3vqFWSWVXmvRFb2iEOU8U02SAQ6mHGSJvqQw3URD9wFLggpqFKTa+RkQq/W0egIEin5Sa/mhq0VKE0UvcpP+97goqbeUE/FqHXv90km8jsSqbolmEd6uFK4R0HPj11gYg+O3yQQywrj1Da6Owm89S8hqu0GdYqnbk2LQSDH4YorBhioGbVSx20ix+2kU/XF4eXRcoxhuoBi2QDFspBi2SREXAh4J742a7kml10il9wUNuWfw/cbg+1/Qah8N/l5yRqe10I26xbgnJVbRPx8N9jsW8rayrMesrc7CvC32RYzNYcBEwUGJ+GrBlY0TTJ/qFa8sm243j2kVon7YS8cc33LMuFqZrO/JibHQBEfoV4Loo8JqbIROr98dmYcpt0q99JTv7vaEb2Y6ZkxSJtEdihFXG2X9aY+Nh8Mrl7aoC5TiaxxFiG7JhFp85QXB0+o2kasyCMhxJveZDcv+QXV5M3GprduaTfeE1S/DjlTa989DZraiDED9f6NWxVhVUnWFpqOuRvqpqYS7XC/5IJfMJMccr+1WQWfDk9Vpo58q6utZtQ6O9nAW2dm5nZoS3VqzfWZ6rmj09rSh0uFfHDbDfeOsWdrvHrUl0P9s0taZr6fU2FuZs+XS/d0xs1/i7A9QSwMECgAAAAAALWD9XAAAAAAAAAAAAAAAAAkAAABkb2NQcm9wcy9QSwMECgAAAAgALWD9XPgrwD83AQAAgwIAABEAAABkb2NQcm9wcy9jb3JlLnhtbKWS0W7CIBSGX6XhvqXUpG6kxWRbvJrJkmm27I7AUckKJcCsvv1o1aqZd7uE/+PLf05bzfa6SXbgvGpNjUiWowSMaKUymxqtlvP0ASU+cCN50xqo0QE8mrFKWCpaB2+uteCCAp9Ej/FU2BptQ7AUYy+2oLnPImFiuG6d5iEe3QZbLr75BnCR5yXWELjkgeNemNrRiE5KKUal/XHNIJACQwMaTPCYZARf2ABO+7sPhuSK1CocLNxFz+FI770awa7rsm4yoLE/wZ+L1/dh1FSZflMCEKukoMIBD61jK5MarkFW+OqyX2DDfVjETa8VyKfDFfc363EHO9V/JUYGYjxWp6GPbpBJLEuPo52Tj8nzy3KOWJEXZZpP0+JxSQqaE1qU2bScfvXVbhwXqT6V+Jf1LGFD89sfh/0CUEsDBAoAAAAIAC1g/VweKelacAIAAGQMAAASAAAAd29yZC9udW1iZXJpbmcueG1szZdLbtswEIavInDvUHLkB4QoQdsghYu+gKYHoCXaJsIXSEqKz9BFd+22Z+tJOpQs+VEgsGUE8Ma0ODPf/BQ5Q+jm7lnwoKTGMiVTFF2FKKAyUzmTyxR9f3wYTFFgHZE54UrSFK2pRXe3N1UiCzGnBtwCkSWzpVSGzDk4VFEcVNEoqHQUowDo0iaVzlK0ck4nGNtsRQWxV4JlRlm1cFeZElgtFiyjuFImx8MwCut/2qiMWgs53hFZEtvixP80pakE40IZQRw8miUWxDwVegB0TRybM87cGtjhuMWoFBVGJhvEoBPkQ5JG0GZoI8wxeZuQe5UVgkpXZ8SGctCgpF0xvV1GXxoYVy2kfGkRpeDbLYji8/bg3pAKhi3wGPl5EyR4o/xlYhQesSMe0UUcI2E/Z6tEECa3iXu9mp2XG41OAwwPAXp53ua8N6rQWxo7jzaTTx3LF/0JrM0m7y7Nnifm24poinzLIXPrDMnc50IEe0+zHFoX8m0nMRS6lfGTTXd6s3DUvDWUPKUorCmi4I59pCXlj2tNAVQSDgrXc8PyT97GvQ1h78tLDg4MBh9dJ3BQhlDLJfUpvU+dr8VETRw0xwfRTc4LzqnriI/0uTP9/f2zm/+QtbOcLjbu+qvxA5M52Px0iiZDryRZEbmsm/T1OPS+eOOMa9ah+Oh1xP84VXwUxz3UD19F/a8/p6ofRuMe6q8v5OAMp9Me6uMLOTkgtof60YWcnPi6T9WOL+TkjMI+VTu5FPWTPlU7vRD14/i4qsV7N+JGVVD/NtfjwQ06yw8WAZQv8CEAtyDdufO6Je/YtlF4L6x+lj453vk+uP0HUEsDBAoAAAAAAC1g/VwAAAAAAAAAAAAAAAAGAAAAX3JlbHMvUEsDBAoAAAAIAC1g/Vwfo5KW5gAAAM4CAAALAAAAX3JlbHMvLnJlbHOtks9KAzEQh18lzL0721ZEpGkvUuhNpD5ASGZ3g80fJlOtb28oilbq2kOPmfzmyzdDFqtD2KlX4uJT1DBtWlAUbXI+9hqet+vJHayWiyfaGamJMvhcVG2JRcMgku8Rix0omNKkTLHedImDkXrkHrOxL6YnnLXtLfJPBpwy1cZp4I2bgtq+Z7qEnbrOW3pIdh8oypknfiUq2XBPouEtsUP3WW4qFvC8zexym78nxUBinBGDNjFNMtduFk/lW6i6PNZyOSbGhObXXA8dhKIjN65kch4zurmmkd0XSeGfFR0zX0p48jGXH1BLAwQKAAAACAAtYP1c0nf8t20AAAB7AAAAGwAAAHdvcmQvX3JlbHMvZm9vdGVyMS54bWwucmVsc02MQQ4CIQxFr0K6d4oujDHDzG4OYPQADVYgDoVQYjy+LF3+vPf+vH7zbj7cNBVxcJwsGBZfnkmCg8d9O1xgXeYb79SHoTFVNSMRdRB7r1dE9ZEz6VQqyyCv0jL1MVvASv5NgfFk7Rnb/wfg8gNQSwMECgAAAAgALWD9XDHFkUDWAQAAtQUAABAAAAB3b3JkL2Zvb3RlcjEueG1spZRZbtswEIavQvDdIiVnq2A5EOy6aNEWAZoegKEpi4m4YEhLbZ96lh6tJwllLbZbIHDiF1Kc4XzzzwzE2e0PVaFagJNGZziOKEZCc7OWepPh7/eryQ2+nc+atPCAwlXt0sbyDJfe25QQx0uhmIuU5GCcKXzEjSKmKCQXpDGwJgmN6e7LguHCucBdMF0zh3uc+p9mrNDBWRhQzIcjbIhi8LS1k0C3zMsHWUn/M7Dp1YAxGd6CTnvEZBTUhqSdoH4bIuCUvF3I0vCtEtrvMhIQVdBgtCul3ZfxVlpwlgOkfqmIWlV4HEF8cd4MlsCasO2Bp8hfd0Gq6pS/TIzpCRNpEWPEKRKOcw5KFJN6n/hNrTlobnz5OkDyL8BuzhvOBzBbu6fJ82gf9dPI0uJVrH7Ih6W588R8K5kVuH1Q7G65g3Z75KhJa1ZlmIf/QgAm8xkZvd3Sf6+M9i7cZo7L0JgFq+QDSBws3B0dBXM+d5IdGctcu4Mo0iK5qQwM+W/yd9Ocdg73a7DGF4Nl4Y5tZFTm2xalzjIeemxBOAF1qHQFxjP0JVjXBr3XG6FLBpKhz/fLHKG/v/8gtPh69wkl19H0aholyZRQSuPJNW3ZvsvQdWO3hnd4/gxQSwMECgAAAAgALWD9XMCyc5ujAQAAuAgAABMAAABbQ29udGVudF9UeXBlc10ueG1stVbLTsMwEPyVKFfUuHBACLXlwOMIHOADXHuTGmKvZW8K/D3r9CEFmlKguWU9MzsT70bK5Ord1tkSQjTopvlpMc4zcAq1cdU0f366G13kV7PJ04eHmDHVxWm+IPKXQkS1ACtjgR4cIyUGK4nLUAkv1ausQJyNx+dCoSNwNKLUI59NbqCUTU3Z9eo8tZ7mxia+d1We3b7z8SpOqsVexYuHrqQ9+LXmJ8nc+o4i1fsVlSk7ilTvV8RldcL32FHxWa9Kel8bJYmJYun0lzmM1jMoAtQtJy6Mj98MGI0HOXwVpvqPybAsjQKNqrEsKXBeNpHZoO+4SccENVF7bQ+8ocFo+I/PGwbtAyqIkZfb1sUWsdK41c08ykD30nJvkehiS1m/7iA5In3UEHcHWGH/st8sgsIAIzb2EMjs8OOAj4xGkYjHfGHVREJ7mHVLPaY5pG3SoA+y59aDTto1dg6Bn3cPewsPGqJEJIfUt3FbeNAQPJM9GTbosJ8dEPFT34e3RgeNoNAmoCfCBh14G7iRnNfQtw1rePCVhNC/jxBON/6i/RWZfQJQSwMECgAAAAgALWD9XFh52yKSAAAA5AAAABMAAABkb2NQcm9wcy9jdXN0b20ueG1snc5BCsIwEIXhq5TZ21QXIqVpN+LaRXUf0mkbaGZCJi329kYED+Dy8cPHa7qXX4oNozgmDceyggLJ8uBo0vDob4cLFJIMDWZhQg07CnRtc48cMCaHUmSARMOcUqiVEjujN1LmTLmMHL1JecZJ8Tg6i1e2q0dK6lRVZ2VXSewP4cfB16u39C85sP28k2e/h+yp9g1QSwMECgAAAAgALWD9XOL8ndqTAAAA5gAAABAAAABkb2NQcm9wcy9hcHAueG1snc5BCsIwEIXhq4TsbaoLkdK0G3HtoroPybQNNDMhE0t7eyOCB3D5+OHjtf0WFrFCYk+o5bGqpQC05DxOWj6G2+EiBWeDziyEoOUOLPuuvSeKkLIHFgVA1nLOOTZKsZ0hGK5KxlJGSsHkMtOkaBy9hSvZVwDM6lTXZwVbBnTgDvEHyq/YrPlf1JH9/OPnsMfiqe4NUEsDBAoAAAAIAC1g/VycicmRzgEAAK0GAAASAAAAd29yZC9mb290bm90ZXMueG1s1ZTNTuMwEMdfJfK9dVIBWkVNOYBA3BDdfQDjOI2F7bFsJ6Fvv5PETbosqgo9cYm/Zn7zn5nY69t3rZJWOC/BFCRbpiQRhkMpza4gf34/LH6RxAdmSqbAiILshSe3m3WXVwDBQBA+QYLxeWd5QeoQbE6p57XQzC+15A48VGHJQVOoKskF7cCVdJVm6TCzDrjwHsPdMdMyTyJO/08DKwweVuA0C7h0O6qZe2vsAumWBfkqlQx7ZKc3BwwUpHEmj4jFJKh3yUdBcTh4uHPiji73wBstTBgiUicUagDja2nnNL5Lw8P6AGlPJdFqRaYWZFeX9eDesQ6HGXiO/HJ00mpUfpqYpWd0pEdMHudI+DfmQYlm0syBv1Wao+Jm118DrD4C7O6y5jw6aOxMk5fRnszbxOov9hdYscnHqfnLxGxrZvEGap4/7Qw49qpQEbYswaon/W9Njp+cpMvD3qKFF5Y5FsAR3JJlQRbZYGiHz7PrB28ZxwhowKog8HanvbGSfc6rq2nx0vQhWROA0M2aTu7jJ863Ya/66C1TBXmIal5EJRy+mSI6RuNqPo77E26SPR3QQTOdvT5Nl4MJ0jTDK7P9mHr6EzL/NINTVTha+M1fUEsDBAoAAAAIAC1g/VzSd/y3bQAAAHsAAAAdAAAAd29yZC9fcmVscy9mb290bm90ZXMueG1sLnJlbHNNjEEOAiEMRa9CuneKLowxw8xuDmD0AA1WIA6FUGI8vixd/rz3/rx+824+3DQVcXCcLBgWX55JgoPHfTtcYF3mG+/Uh6ExVTUjEXUQe69XRPWRM+lUKssgr9Iy9TFbwEr+TYHxZO0Z2/8H4PIDUEsDBAoAAAAIAC1g/Vw/So6NwQEAAJIGAAARAAAAd29yZC9lbmRub3Rlcy54bWzNlNtu4yAQhl/F4j7BjrrVyorTix5Wvaua3QegGMeowCDA9ubtd3wIzrZVlDY3vTGnmW/+mTGsb/5qlbTCeQmmINkyJYkwHEppdgX58/th8ZPcbNZdLkxpIAifoL3xeWd5QeoQbE6p57XQzC+15A48VGHJQVOoKskF7cCVdJVm6TCzDrjwHuG3zLTMkwmn39PACoOHFTjNAi7djmrmXhu7QLplQb5IJcMe2en1AQMFaZzJJ8QiCupd8lHQNBw83DlxR5c74I0WJgwRqRMKNYDxtbRzGl+l4WF9gLSnkmi1IrEF2dVlPbhzrMNhBp4jvxydtBqVnyZm6Rkd6RHR4xwJ/8c8KNFMmjnwl0pzVNzsx+cAq7cAu7usOb8cNHamyctoj+Y1soz4FGtq8nFq/jIx25pZvIGa5487A469KFSELUuw6kn/W5OjFyfp8rC3aOCFZY4FcAS3ZFmQRTbY2eHz5PrBW8YxABqwKgi83GlvrGSf8uoqLp6bPiJrAhC6WdPoPn6m+TbsVR+9Zaog96OYZ1EJh++jmPwmWxFPp+0Ii6LjAR0U0+j0UaocTJCmGR6Y7du00++f9Yf6T1RgnvvNP1BLAwQKAAAACAAtYP1c0nf8t20AAAB7AAAAHAAAAHdvcmQvX3JlbHMvZW5kbm90ZXMueG1sLnJlbHNNjEEOAiEMRa9CuneKLowxw8xuDmD0AA1WIA6FUGI8vixd/rz3/rx+824+3DQVcXCcLBgWX55JgoPHfTtcYF3mG+/Uh6ExVTUjEXUQe69XRPWRM+lUKssgr9Iy9TFbwEr+TYHxZO0Z2/8H4PIDUEsDBAoAAAAIAC1g/VxNn8rKoQEAAHMFAAARAAAAd29yZC9zZXR0aW5ncy54bWyllN1u2zAMhV/F0H0iu1iLwahbdCvW9WLYRbcHYCXZFiJRgiTby9uPjuO4P0CRNFeSQfE7R6TF69t/1mS9ClE7rFixzlmmUDipsanY3z8/Vl9ZFhOgBONQVWyrIru9uR7KqFKiQzEjAMZy8KJibUq+5DyKVlmIa6tFcNHVaS2c5a6utVB8cEHyi7zIdzsfnFAxEug7YA+R7XH2Pc15hRSsXbCQ6DM03ELYdH5FdA9JP2uj05bY+dWMcRXrApZ7xOpgaEwpJ0P7Zc4Ix+hOKfdOdFZh2inyoAx5cBhb7ZdrfJZGwXaG9B9doreGHVpQfDmvB/cBBloW4DH25ZRkzeT8Y2KRH9GREXHIOMbCa83ZiQWNi/CnSvOiuMXlaYCLtwDfnNech+A6v9D0ebRH3BxY47s+gbVv8surxfPMPLXg6QVaUT426AI8G3JELcuo6tn4W7Nx4kgdvYHtNxCbhmqBcpfGx5DqFd6h/C3lTwWSplk2lD2YitVgomK7M9OUWHZP0wCbTxaXjLYIlqRfDZRfTqox1IUTSj5K8kWTL/Py5j9QSwMECgAAAAgALWD9XIuGOcTFAQAAxggAABEAAAB3b3JkL2NvbW1lbnRzLnhtbKXU3XLiIBgG4FtxOFeSWFM307Qnne30eNsLoIDCNPwMoNG7X1IlSZedToJH6iTfk5fXwMPTSTSLIzWWK1mDfJWBBZVYES73NXh/+73cgoV1SBLUKElrcKYWPD0+tBVWQlDp7MID0lb4VAPmnK4gtJhRgexKcGyUVTu38vdCtdtxTCExqPU2LLL8DmKGjKMn0Bv5bGQDf8FtDBUJUJ7BIo+p9WyqhF2qCLpLgnyqSNqkSf9ZXJkmFbF0nyatY2mbJkWvk8ARpDSV/uJOGYGc/2n2UCDzedBLD2vk+AdvuDt7MysDg7j8TEjkp3pBrMls4R4KRWizJkFRNTgYWV3nl/18F726zF8/woSZsv7LyLPCh247f60cGtr4LpS0jGvb15mq+YssIMefFnEUTbiv1fnE7dIqQ7q+sq9v2ihMrfUdPl+qHMAp8a/9i+aS/Gcxzyb8Ix3RT0yJ8P2ZIYnwb+Hw4KRqRuXmEw+QABQRUGI68cAPxvZqQDzs0M7hE7dGcMre4WTkpIUZAZY4wmYpRegVdrPIIYYsG4t0XqhNz53FqCO9v20jvBh10IPGb9Neh2OtlfMWmJX/tq7tbWH+MKQpgI9/AVBLAwQKAAAACAAtYP1c0nf8t20AAAB7AAAAHAAAAHdvcmQvX3JlbHMvY29tbWVudHMueG1sLnJlbHNNjEEOAiEMRa9CuneKLowxw8xuDmD0AA1WIA6FUGI8vixd/rz3/rx+824+3DQVcXCcLBgWX55JgoPHfTtcYF3mG+/Uh6ExVTUjEXUQe69XRPWRM+lUKssgr9Iy9TFbwEr+TYHxZO0Z2/8H4PIDUEsDBAoAAAAIAC1g/Vxj7V7WHQEAAEMDAAASAAAAd29yZC9mb250VGFibGUueG1sndHdbsIgFAfwVyHcK7WZjWms3ixLdr89AAK1RA6n4eDUtx+ttmvijd0VEPL/5Xxs91dw7McEsugrvlpmnBmvUFt/rPj318diwxlF6bV06E3Fb4b4fre9lDX6SCylPZWgKt7E2JZCkGoMSFpia3z6rDGAjOkZjgJkOJ3bhUJoZbQH62y8iTzLCv5gwisK1rVV5h3VGYyPfV4E45KInhrb0qBdXtEuGHQbUBmi1DG4uwfS+pFZvT1BYFVAwjouUzOPinoqxVdZfwP3B6znAfkTUChznWdsHoZIyalj9TynGB2rJ87/ipkApKNuZin5MFfRZWWUjaRmKpp5Ra1H7gbdjECVn0ePQR5cktLWWVoc62F2n1x3sPsy2NACF7tfUEsDBAoAAAAIAC1g/VzSd/y3bQAAAHsAAAAdAAAAd29yZC9fcmVscy9mb250VGFibGUueG1sLnJlbHNNjEEOAiEMRa9CuneKLowxw8xuDmD0AA1WIA6FUGI8vixd/rz3/rx+824+3DQVcXCcLBgWX55JgoPHfTtcYF3mG+/Uh6ExVTUjEXUQe69XRPWRM+lUKssgr9Iy9TFbwEr+TYHxZO0Z2/8H4PIDUEsDBAoAAAAAAC1g/VwAAAAAAAAAAAAAAAALAAAAd29yZC9tZWRpYS9QSwMECgAAAAgALWD9XCT9uJzVDQAA0Q4AADcAAAB3b3JkL21lZGlhLzAwNzdlZDljODE4OGRmYjI0NDc3ZDVkYjk2MDlmZjRjNTQ1ZTA3ZjEuanBnnVZ7OJT5238GhbCl6DDIpshZzlthOlEpISEzDm9ZYiakHENTbc4rK2GxmhCTw7AYMzHMrIYUSQ5jZDCZHNJgjMEMY+b5jb323d0/3j/e972f676u7/Pc9/e+ns/nvu/v9waHwc/ADkeH8w4ABAIBmiQPADKA04Dc1q2yW7fIycrKysvLbVNUUVJUUFDct3PXdhUNqOZ+Dai6+veHjA9/r2V4UF1d11rP8IiphYWF5uGjtj+Y2RibW5htBoHIy8srKijuVVLaa3ZA/YDZ/1nAPwBlOeBH4K00RAuQUoZIK0PAdkATACBbIH8K8JdApKRltmyVlZPfpiBxaNwBSEGkpaVkpLdskZGRWBMkdkBGecvOA6Ynt+5yvSarFa5i9iCrWO7gqbo21ct9i4fMr99+KL9t9569+6DaOod19fQtLK2sfzh67PQZe4ez5847ul1x9/C86gX3/zEg8EZQMPJORGRUdEzs3Z8eJSYlp6SmPcl+mpOb92t+QUnpi7Jy7MuKyvoGfCOB+Kqp+TW1veNN59t3Xf0Dg7Qh+vCnkQnWl8mp6Zmvs9+4S7zllVW+YG19ExcEkIb8t/yPuJQluKRkZKRlZDdxQaSiNx2UZbYcMN2686Sr7LXwXVpmD+RUTmUV17XJHzS/vKh6/Xbftt2HLCa0uZvQ/kT2vwP28P+F7G9g/+AaARSlIZLkSSsDMID/TbfsuUxJbhrgqBckWaSZn1Aqqz3O7W0PWGz8NjxyFTf/pteUb0dzXqDLw95kKjuJS/q7mIs3qqfguuzShzgvX10+RaCkRhe+Jt/GsJzTFsSrVVMmItNE+4BellHojNguMp2+/p72W1U7G5vtkTu3fj2OjYgcs23Xhc2WPrjKt7NcpKXdAwFEg+Z5d8oydK2D8WGtKJ8hKh9zqQ8RVhFK2XZnr5hdWrKdZrPoiUYNhNmF7ZL4Vf6vyaYDNggl6nBsytA84Ux+FLGutiAf5UyFijqTGt9ayhEt0FEsGAfLdYX265Bd5dz41naVZZVW1h+RVi5jjHO1il2HMvIEib41DW3DumUl93emwXUhfy/0gvWCgJI03U2FGBggBueXnaCcy83HrxEcslrZHpWjjI5ImVyeQ61azVBGs/KJM2sebPdXtEi1dGFp/kzWC9iGk2uTU0aTHRu9nY+q5ILAg6nFpKoZhAE56xYINKHXJpmjYaJckklSQQI5Z0KNs5NZORcZUpgn9qoFARUadU70ZdauSc4l/cFUVLNvVQzz60jIJHMB0eAhwtxv8YwU155pcgeBcoYbCJyqSeJ/MSbUqxIGDFmZAh+idbdX/4Yr16dzrOWh5vpMKEVoqUmyyPD+UaySFTow4ZwaeG3GqKwZBIhWHNFWwjWBacQ9VGugHAiMJhZK4iFZC/EoFoJCwY2jSbtCQOAjCvpN6NIcQt9Dz0bTz+e/nE72d/Z/nMc4t6oW9hP8ciIqw3H1i6in0sCDMeH8aELTNn1Ucz3E3PKOn1yLx8L2ZBPttYEZvCEzFp0K+2zJZlKXPJ1+aMNtl1VF7JJfuhTgY32hvq/6YP3vxAtxV8VzKL4HXk8P5fZItyQN+Lc6wh3hf7/o6QXrSmvC++Yae8wSjolNrMYDqCYr+1cmJQyunLOIO64pf9X47fVE302iUEuVIDBHm4+r8/bM3FDdno7HfZxzcvxkHmJdK1QSd1+5ZP04+RIUqbort7N3nsCyxfEpiRSSC1WJ+q5eLbY8bP2GmiW8f7mQzdlBLY41UmL3THZl8LOFTH4XHRdD7XCeMA8uDjrSNn1yGyfYM4q5UDvUpGprcpD5TI9X5uzRa9WwSEwJBQFaUB5WhE3Mk/CMe2kHjSmfgk1C69D04KfuCEzKft3BPeJTtd8tfHxT4cOhmkBHkYhlp8ufu6MwLQ+DEdM56FT0ODzc5XbdD2nDzUdmPrYP2O0UPB5fVaAN3sQdTnyU4nknBLn66nSKa6D+jwye/1B5Wc2rtrP+M2x6Ueydi3AJgaU7/qrtv2v83wUPvNhsg58unD+h3kNPXh6ObGq5oPpY5GpUYMnYQIvykGtkvvurwRUEidHDROYGGuM2jERMgVJCsK8G16/NLyDWxqm8Q0gAgcxJvwkYbwAE0qqrHLi2is0t19zGbVgBIeMRBqTocXecj/kluu+0Qo+1cyuPFfJOi0Hsoo1tQBc5RYrrK+/94vi7hRTuNAh8Qg0TtETM3Dqsg5j7KfX9mlp5Ttcx1fnx4lKGN/PT+YCIbh6D4m6Jc8J6erFMg0rSZFRc7YF/q7aRthGgfaO3Kni1IdbTeuj3d6JPge7Ij7UF9wzKf2XuPbv6ukeEmaSuZjaAQDWjkTwPJMiKGsrXfMNWM7A5JEni8MZj4zmWjaKiaU69D9WO2Tr5FkcgR3A9iiYY6Ee5RLu9QsTVsW9CEBggLIzW1Q9FJxp9dk7xVXeWd4paOvKIyZm4AgLdASCAtMVDlqdoK7hEklN0uOKO2vHYInekEcxzoNEi8PfBkNBVEUcnIDtMxPOVv4MK58JYPav5cXcsjvn+tqTfjqFCrZhP0bGFggqb6M/tgpBwbEwydc9Ah5BpmBxfNDwGAlJKohGAXyHpi8NBU4M3v/xyq61GxWygeMiCnZq711LN61BYc9bFaniLdSd5W19eW3/Mwp5mYdSLj6N44299VU58sg9l3gNL5Uh/PRadZEHRGO2JyPMYWvJ+NU72625CkyThV7UHjeBzI7hBpVut13ied8UqbBpBqJ4e5b+075aQgWSRvfpXcM6XuPaYzFB/g7DIymNdIDAuXgIBphEIPKa3xjs90SPvQYVW49QYBgZ3a6aef1d4fmAuosdYQcNW1ehA5k19HCa3XHhRayJkJKi9I1o/dC0FBKJaeBOwVN8Z6s7gY8VLocuPfMbDuCe6mxDitRq319DWzlQ7q7PVLfYhC0Y1Wx5z1lgUfUnnL0PmB7xPKtZ1Hqp78ST+ZTXjQoK+Z871n1uqVa8E5L/qiLnRrpBO2SHAetJuGp+qfVSSOccmcXpIERgBR/wTnHxkyDljxU8uyIp/mTELH/RDvlpiKLJBwIYHAhAksGzP7Uwcn5nrrp0djwpvmFfwnJaQHWtTsaJw9YV4Llu/gAr7zgYaAQJ16NfHAybn33AM+Bg+fbzWkAuNCe1kjZcUrGYbF18viDelKpqa50S6M/joT8+3h/kgnIPiaa2EoDsN73l5oZwF2q/G8wueR7ztTUS3f7GMyczAtyrbaE+cwxduxfdzVgKycJMep3nGQ2OikRGuM7UHRoVr01NsDhuuXyr21yxautijXkmJ9YLft4UHlxIf9VikZdUavh+WqsPFoyaq2JR6OHsknq45ikjsiX2MrXTibvdaDE2/y9mKQfuLimOyCMyjedHdkgyCwBfaCJKp2KL38mv/VWo5fEQ1k2+9o4aeQUkkushGXvCNXEz3Ig3F790y94aFOp7cyCsUw/VFI8vwfw5zxyXb6D/We0DgzQVN5TK8SZn95NrP/cacHATM6O68NhCL6fCTR/oeXmx9hilmnOovY/72rtXfh4SQHCs1Z68IQli4w+59KxwF6ja/s2Hbxk480Q/J90Sh67uYHb4r9kBMIGmR15FbMLjfcHE5t6L6ok03O8hvyquQMz/Gz6E003zDpATCzpGyCi6q0x97EgQMvxhIOmikVFBIqxrccIu2bJjt1mgjJnnlB4Ydv6GWENXoRw8oYFLpbvCLrwOS+bdr3zPhwMy9A4JADmJxV1CBdkugy+JaOL7wij6RX/Vs0MaA9VRgjY2Bd9HUXGCkJtF6ZXVcPpkv4vm8yYtG61RFouJJ8SmlDC0QqCoMD/sV/aXF3eiCy7nIKgrRz5txMrSutcVqnvhBo1foZ91NuG8zxP78uDJep64D12s04lCvUzC8ZNPL8l4Agc4q3H5NrtFMW8UGLA5JYRiErV7ZfzvnFm6piLlA92EdnWrs08mV1dPCo80IzLUgEvk3j6RnE8uCIm3ePb/W6TWMgM91kX0qyPzD7nsSoc/PXmtkZXmlfyVsXH+j0BOzSklrZL72HLmRgRedPVOfMWchUsUdELGqLDBWTCFbSGsoRULf5x27af0z76zFJRAIusIgiXL9JgI2/+20G2nwOe1gUpdqhNWdc+6MIMWyKAzphDo6kaw6CxtW7mZlWB3HIVnndV4lzFPN0Q1eURSlSMycc50NO+BlEL3+Qqlf2Oi+fcSQMOfNQaXWhdCKQiudfum9ELubFpg9tZZPoPkpjX3b6dUbVnkoPUEGYzVPMSO+p0SziWLpE+pYBtmwkfgpKbPkUKvdaKgZbJzhzlmGQWfFdq3c7zKuYZkJJR+OB8alZxqGuvv0EEVTNbkf1oXinRWsZ8G7aWVZ/jLx9f9lrIpu7uahWxFuNlUstXO/ZCnm8sxNhI7viJvDE8Dr3R28/yjXLhXb+ib7enNGbnlDtRVf08ChSiS5zJCYilmMsg1KdjUQG4YNuuudLNIuzvvwxF48H1kwXZGAigsiuDpeI4pE01aSevgwiv7q1CkZIh1G8ZCZe9qC2NV0JY2No5GrGm72qf4N+WS2CR+xAQKqbi3OadCp1AGdxj48PnyqJ0HxfQ9RQMDdzVGB9MZ4UqOeqSyKarIDvfph+UVkXt03Wt/lyx8h9iouUpf7/rkpLZ/ux5MkHyF4t4vnGLdLp2btVQCSvU6H/ZqDbsl9DbwrXuJl+jQEK9mkZq/2p6uLxLI7Tfe51F+6Hfz0H1BLAQIUAAoAAAAAAC1g/VwAAAAAAAAAAAAAAAAFAAAAAAAAAAAAEAAAAAAAAAB3b3JkL1BLAQIUAAoAAAAAAC1g/VwAAAAAAAAAAAAAAAALAAAAAAAAAAAAEAAAACMAAAB3b3JkL19yZWxzL1BLAQIUAAoAAAAIAC1g/VyDDJMKNQEAAEcFAAAcAAAAAAAAAAAAAAAAAEwAAAB3b3JkL19yZWxzL2RvY3VtZW50LnhtbC5yZWxzUEsBAhQACgAAAAgALWD9XIoRD9eNDAAAhrQAABEAAAAAAAAAAAAAAAAAuwEAAHdvcmQvZG9jdW1lbnQueG1sUEsBAhQACgAAAAgALWD9XJjTxsINAwAAExEAAA8AAAAAAAAAAAAAAAAAdw4AAHdvcmQvc3R5bGVzLnhtbFBLAQIUAAoAAAAAAC1g/VwAAAAAAAAAAAAAAAAJAAAAAAAAAAAAEAAAALERAABkb2NQcm9wcy9QSwECFAAKAAAACAAtYP1c+CvAPzcBAACDAgAAEQAAAAAAAAAAAAAAAADYEQAAZG9jUHJvcHMvY29yZS54bWxQSwECFAAKAAAACAAtYP1cHinpWnACAABkDAAAEgAAAAAAAAAAAAAAAAA+EwAAd29yZC9udW1iZXJpbmcueG1sUEsBAhQACgAAAAAALWD9XAAAAAAAAAAAAAAAAAYAAAAAAAAAAAAQAAAA3hUAAF9yZWxzL1BLAQIUAAoAAAAIAC1g/Vwfo5KW5gAAAM4CAAALAAAAAAAAAAAAAAAAAAIWAABfcmVscy8ucmVsc1BLAQIUAAoAAAAIAC1g/VzSd/y3bQAAAHsAAAAbAAAAAAAAAAAAAAAAABEXAAB3b3JkL19yZWxzL2Zvb3RlcjEueG1sLnJlbHNQSwECFAAKAAAACAAtYP1cMcWRQNYBAAC1BQAAEAAAAAAAAAAAAAAAAAC3FwAAd29yZC9mb290ZXIxLnhtbFBLAQIUAAoAAAAIAC1g/VzAsnObowEAALgIAAATAAAAAAAAAAAAAAAAALsZAABbQ29udGVudF9UeXBlc10ueG1sUEsBAhQACgAAAAgALWD9XFh52yKSAAAA5AAAABMAAAAAAAAAAAAAAAAAjxsAAGRvY1Byb3BzL2N1c3RvbS54bWxQSwECFAAKAAAACAAtYP1c4vyd2pMAAADmAAAAEAAAAAAAAAAAAAAAAABSHAAAZG9jUHJvcHMvYXBwLnhtbFBLAQIUAAoAAAAIAC1g/VycicmRzgEAAK0GAAASAAAAAAAAAAAAAAAAABMdAAB3b3JkL2Zvb3Rub3Rlcy54bWxQSwECFAAKAAAACAAtYP1c0nf8t20AAAB7AAAAHQAAAAAAAAAAAAAAAAARHwAAd29yZC9fcmVscy9mb290bm90ZXMueG1sLnJlbHNQSwECFAAKAAAACAAtYP1cP0qOjcEBAACSBgAAEQAAAAAAAAAAAAAAAAC5HwAAd29yZC9lbmRub3Rlcy54bWxQSwECFAAKAAAACAAtYP1c0nf8t20AAAB7AAAAHAAAAAAAAAAAAAAAAACpIQAAd29yZC9fcmVscy9lbmRub3Rlcy54bWwucmVsc1BLAQIUAAoAAAAIAC1g/VxNn8rKoQEAAHMFAAARAAAAAAAAAAAAAAAAAFAiAAB3b3JkL3NldHRpbmdzLnhtbFBLAQIUAAoAAAAIAC1g/VyLhjnExQEAAMYIAAARAAAAAAAAAAAAAAAAACAkAAB3b3JkL2NvbW1lbnRzLnhtbFBLAQIUAAoAAAAIAC1g/VzSd/y3bQAAAHsAAAAcAAAAAAAAAAAAAAAAABQmAAB3b3JkL19yZWxzL2NvbW1lbnRzLnhtbC5yZWxzUEsBAhQACgAAAAgALWD9XGPtXtYdAQAAQwMAABIAAAAAAAAAAAAAAAAAuyYAAHdvcmQvZm9udFRhYmxlLnhtbFBLAQIUAAoAAAAIAC1g/VzSd/y3bQAAAHsAAAAdAAAAAAAAAAAAAAAAAAgoAAB3b3JkL19yZWxzL2ZvbnRUYWJsZS54bWwucmVsc1BLAQIUAAoAAAAAAC1g/VwAAAAAAAAAAAAAAAALAAAAAAAAAAAAEAAAALAoAAB3b3JkL21lZGlhL1BLAQIUAAoAAAAIAC1g/Vwk/bic1Q0AANEOAAA3AAAAAAAAAAAAAAAAANkoAAB3b3JkL21lZGlhLzAwNzdlZDljODE4OGRmYjI0NDc3ZDVkYjk2MDlmZjRjNTQ1ZTA3ZjEuanBnUEsFBgAAAAAaABoAoQYAAAM3AAAAAA=="

_NAVY=_col.HexColor("#24344D"); _GRAY=_col.HexColor("#666666"); _LGRAY=_col.HexColor("#EDEDED"); _LN=_col.HexColor("#C9CDD3")
_MESES=["JANEIRO","FEVEREIRO","MARÇO","ABRIL","MAIO","JUNHO","JULHO","AGOSTO","SETEMBRO","OUTUBRO","NOVEMBRO","DEZEMBRO"]
def _rs(v): return "R$ " + f"{float(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")
def _num_limpo(s):
    d=re.sub(r"\D","",str(s or "")); d=d.lstrip("0"); return d or ("0" if re.search(r"\d",str(s or "")) else "")
def _data_iso(s):
    m=re.search(r"(\d{2})/(\d{2})/(\d{4})",str(s or ""))
    if m: return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m=re.search(r"(\d{4})-(\d{2})-(\d{2})",str(s or ""))
    return m.group(0) if m else None
def _mes_da_data(s):
    iso=_data_iso(s)
    if iso:
        y,mo,_=iso.split("-"); return f"{_MESES[int(mo)-1]} {y}"
    h=datetime.date.today(); return f"{_MESES[h.month-1]} {h.year}"

_EU=["zero","um","dois","três","quatro","cinco","seis","sete","oito","nove","dez","onze","doze","treze","quatorze","quinze","dezesseis","dezessete","dezoito","dezenove"]
_ED=["","","vinte","trinta","quarenta","cinquenta","sessenta","setenta","oitenta","noventa"]
_EC=["","cento","duzentos","trezentos","quatrocentos","quinhentos","seiscentos","setecentos","oitocentos","novecentos"]
def _ext999(n):
    if n==0: return ""
    if n==100: return "cem"
    p=[];c=n//100;r=n%100
    if c:p.append(_EC[c])
    if 0<r<20:p.append(_EU[r])
    else:
        d=r//10;u=r%10
        if d:p.append(_ED[d])
        if u:p.append(_EU[u])
    return " e ".join(p)
def _extenso_reais(v):
    v=round(float(v),2);reais=int(v);cent=int(round((v-reais)*100))
    def g(n):
        mil=n//1000;resto=n%1000;o=[]
        if mil:o.append("mil" if mil==1 else _ext999(mil)+" mil")
        if resto:o.append(_ext999(resto))
        return " e ".join([x for x in o if x])
    rp="zero reais" if reais==0 else ("um real" if reais==1 else g(reais)+" reais")
    if cent==0:return rp+"."
    cp="um centavo" if cent==1 else _ext999(cent)+" centavos"
    return f"{rp} e {cp}."

def _oc_hdr(t): return _P(t,_PS("h",fontName="Helvetica-Bold",fontSize=8.5,textColor=_col.white,leading=11))
def _oc_kv(l,v): return _P(f"<b>{l}:</b> {v}",_PS("kv",fontName="Helvetica",fontSize=8.5,textColor=_col.HexColor('#1e2733'),leading=13))

def gera_orcamento_pdf(d):
    buf=io.BytesIO()
    doc=_SDT(buf,pagesize=_A4,leftMargin=16*_mm,rightMargin=16*_mm,topMargin=12*_mm,bottomMargin=14*_mm,title=f"Orçamento {d['num']}")
    W=doc.width; el=[]
    emp=_P("<b>FROTA MACEDO ENGENHARIA LTDA</b><br/><font size=7 color='#666666'>"
        "Eng. Heitor de Oliveira Albuquerque, 295 — Cidade dos Funcionários — Fortaleza/CE<br/>"
        "(85) 2181-1386 • frotamacedoengenharia@gmail.com • CNPJ 27.363.223/0001-70</font>",
        _PS("emp",fontName="Helvetica",fontSize=11,textColor=_NAVY,leading=13))
    dire=_P(f"<font size=8 color='#666666'>{d['data']}<br/>Orçamento nº {d['num']}</font>",_PS("dir",alignment=_TR,fontName="Helvetica",fontSize=8,leading=12))
    try: logo=_IMG(io.BytesIO(_b64.b64decode(_LOGO_ORC_B64)),width=22*_mm,height=11*_mm)
    except Exception: logo=_P("",_PS("x"))
    h=_T([[logo,emp,dire]],colWidths=[24*_mm,W-24*_mm-40*_mm,40*_mm])
    h.setStyle(_TS([("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LINEBELOW",(0,0),(-1,-1),1.2,_NAVY),("BOTTOMPADDING",(0,0),(-1,-1),8)]))
    el+=[h,_SP(1,8)]
    tit=_T([[_P(f"<font size=17 color='white'><b>ORÇAMENTO N° {d['num']}</b></font><br/><font size=8 color='white'>REVISÃO {d.get('revisao',1)}</font>",_PS("t",leading=20))]],colWidths=[W])
    tit.setStyle(_TS([("BACKGROUND",(0,0),(-1,-1),_NAVY),("LEFTPADDING",(0,0),(-1,-1),12),("TOPPADDING",(0,0),(-1,-1),10),("BOTTOMPADDING",(0,0),(-1,-1),10)]))
    el+=[tit,_SP(1,8)]
    obra=_T([[_P(f"<b>OBRA:</b>  MANUTENCAO {d['loja_nome']}  #{d['num']}",_PS("o",fontName="Helvetica",fontSize=9,leading=12))]],colWidths=[W])
    obra.setStyle(_TS([("BACKGROUND",(0,0),(-1,-1),_LGRAY),("LEFTPADDING",(0,0),(-1,-1),10),("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    el+=[obra,_SP(1,10)]
    pv=d["prestador"]; tv=d["tomador"]; cw=(W-6)/2
    pb=[_oc_kv("Nome",pv["nome"]),_oc_kv("CNPJ",pv["cnpj"]),_oc_kv("Forma de pagamento",pv["forma"]),_oc_kv("Data de faturamento",d["data"])]
    tb=[_oc_kv("Nome",tv["nome"]),_oc_kv("CNPJ",tv.get("cnpj") or "—"),_oc_kv("Endereço",tv.get("endereco") or "—"),_oc_kv("Cidade/Estado",tv.get("cidade") or "—")]
    def bloco(tt,ls):
        inner=_T([[_oc_hdr(tt)]]+[[x] for x in ls],colWidths=[cw-2])
        inner.setStyle(_TS([("BACKGROUND",(0,0),(0,0),_NAVY),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
            ("TOPPADDING",(0,0),(0,0),5),("BOTTOMPADDING",(0,0),(0,0),5),("TOPPADDING",(0,1),(-1,-1),2),("BOTTOMPADDING",(0,1),(-1,-1),2),
            ("BOX",(0,1),(0,-1),0.6,_LN),("TOPPADDING",(0,1),(0,1),6),("BOTTOMPADDING",(0,-1),(0,-1),6)]))
        return inner
    pt=_T([[bloco("DADOS DO PRESTADOR",pb),bloco("DADOS DO TOMADOR",tb)]],colWidths=[cw,cw],hAlign="LEFT")
    pt.setStyle(_TS([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(0,0),0),("RIGHTPADDING",(1,0),(1,0),0)]))
    el+=[pt,_SP(1,12)]
    el.append(_P("<b>DISCRIMINAÇÃO DOS ITENS</b>",_PS("di",fontName="Helvetica-Bold",fontSize=9,textColor=_NAVY,spaceAfter=4)))
    data=[["ITEM","DESCRIÇÃO","QUANT.","UNID.","VALOR UNIT.","TOTAL"]]; tg=0.0
    ds=_PS("ds",fontName="Helvetica",fontSize=8.5,leading=11)
    for i,it in enumerate(d["itens"],1):
        vu=round(float(it["valor_unit"])*1.20,2); tot=round(vu*float(it["quant"]),2); tg+=tot
        data.append([str(i),_P(it["descricao"],ds),f"{float(it['quant']):.2f}".replace(".",","),it.get("unid","UN"),_rs(vu),_rs(tot)])
    data.append(["TOTAL GERAL","","","","",_rs(tg)]); n=len(data)-1
    itb=_T(data,colWidths=[13*_mm,W-13*_mm-22*_mm-16*_mm-26*_mm-26*_mm,22*_mm,16*_mm,26*_mm,26*_mm])
    itb.setStyle(_TS([("BACKGROUND",(0,0),(-1,0),_NAVY),("TEXTCOLOR",(0,0),(-1,0),_col.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),8.5),("ALIGN",(2,0),(5,-1),"CENTER"),("ALIGN",(4,1),(5,-1),"RIGHT"),("ALIGN",(0,0),(0,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("GRID",(0,0),(-1,-1),0.5,_LN),("ROWBACKGROUNDS",(0,1),(-1,n-1),[_col.white,_col.HexColor("#F6F7F9")]),
        ("SPAN",(0,n),(4,n)),("BACKGROUND",(0,n),(-1,n),_LGRAY),("FONTNAME",(0,n),(-1,n),"Helvetica-Bold"),("ALIGN",(0,n),(4,n),"RIGHT"),
        ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(1,0),(1,-1),6)]))
    el+=[itb,_SP(1,8)]
    el.append(_P(f"<b>Valor total por extenso:</b>  <i>{_extenso_reais(tg)}</i>",_PS("ex",fontName="Helvetica",fontSize=9,leading=12)))
    el.append(_SP(1,12))
    obs=["Orçamento válido por 7 (sete) dias corridos a partir da data de emissão.","Os valores acima incluem material e serviço de entrega."]
    obt=_T([[_oc_hdr("OBSERVAÇÕES")]]+[[_P("• "+o,_PS("ob",fontName="Helvetica",fontSize=8.5,leading=12))] for o in obs],colWidths=[W-2])
    obt.setStyle(_TS([("BACKGROUND",(0,0),(0,0),_NAVY),("LEFTPADDING",(0,0),(-1,-1),8),("TOPPADDING",(0,0),(0,0),5),("BOTTOMPADDING",(0,0),(0,0),5),
        ("BOX",(0,1),(0,-1),0.6,_LN),("TOPPADDING",(0,1),(0,1),6),("BOTTOMPADDING",(0,-1),(0,-1),6),("TOPPADDING",(0,1),(-1,-1),2),("BOTTOMPADDING",(0,1),(-1,-1),2)]))
    el+=[obt,_SP(1,26)]
    ass=_T([[_P("Frota Macedo Engenharia LTDA",_PS("a1",alignment=_TC,fontSize=8.5,textColor=_GRAY)),
             _P("Aceite do Cliente — Nome / Data",_PS("a2",alignment=_TC,fontSize=8.5,textColor=_GRAY))]],colWidths=[(W-10)/2,(W-10)/2])
    ass.setStyle(_TS([("LINEABOVE",(0,0),(0,0),0.6,_GRAY),("LINEABOVE",(1,0),(1,0),0.6,_GRAY),("TOPPADDING",(0,0),(-1,-1),4),("LEFTPADDING",(0,0),(0,0),20),("RIGHTPADDING",(1,0),(1,0),20)]))
    el+=[ass]
    def _rod(c,dd):
        c.saveState(); c.setFont("Helvetica",7.5); c.setFillColor(_GRAY)
        c.drawCentredString(_A4[0]/2,9*_mm,"Frota Macedo Engenharia LTDA  •  CNPJ 27.363.223/0001-70"); c.restoreState()
    doc.build(el,onFirstPage=_rod,onLaterPages=_rod)
    return buf.getvalue()

# ---- gerador DOCX (usa o modelo Word do usuário como base) ----
def _dx_set(cell, lines):
    p=cell.paragraphs[0]; base=p.runs[0] if p.runs else None
    bold=base.bold if base else None; size=base.font.size if base else None; name=base.font.name if base else None
    for ex in cell.paragraphs[1:]: ex._element.getparent().remove(ex._element)
    for r in list(p.runs): r._element.getparent().remove(r._element)
    for i,ln in enumerate(lines):
        if i>0: p.add_run().add_break()
        rr=p.add_run(ln)
        if bold is not None: rr.bold=bold
        if size is not None: rr.font.size=size
        if name is not None: rr.font.name=name
def gera_orcamento_docx(d):
    from docx import Document
    doc=Document(io.BytesIO(_b64.b64decode(_MODELO_DOCX_B64))); T=doc.tables
    _dx_set(T[0].rows[0].cells[2],[d["data"],f"Orçamento nº {d['num']}"])
    _dx_set(T[1].rows[0].cells[0],[f"ORÇAMENTO Nº {d['num']}",f"REVISÃO {d.get('revisao',1)}"])
    _dx_set(T[2].rows[0].cells[0],[f"OBRA:  MANUTENCAO {d['loja_nome']}  #{d['num']}"])
    itbl=T[4]; total_tr=itbl.rows[-1]._tr; modelo_tr=_dcopy(itbl.rows[1]._tr)
    for row in list(itbl.rows[1:-1]): itbl._tbl.remove(row._tr)
    tg=0.0
    for it in d["itens"]:
        total_tr.addprevious(_dcopy(modelo_tr))
    for i,(row,it) in enumerate(zip(itbl.rows[1:-1],d["itens"]),1):
        vu=round(float(it["valor_unit"])*1.20,2); tot=round(vu*float(it["quant"]),2); tg+=tot
        vals=[str(i),it["descricao"],f"{float(it['quant']):.2f}".replace(".",","),it.get("unid","UN"),_rs(vu),_rs(tot)]
        for c,v in zip(row.cells,vals): _dx_set(c,[v])
    _dx_set(itbl.rows[-1].cells[-1],[_rs(tg)])
    for p in doc.paragraphs:
        if p.text.strip().lower().startswith("valor total por extenso"):
            base=p.runs[0] if p.runs else None
            for r in list(p.runs): r._element.getparent().remove(r._element)
            rr=p.add_run("Valor total por extenso:  "+_extenso_reais(tg))
            if base is not None and base.font.size: rr.font.size=base.font.size
            break
    out=io.BytesIO(); doc.save(out); return out.getvalue()

_ORC_PROMPT=(
"Você recebe um 'DOCUMENTO AUXILIAR DE VENDA - PEDIDO' (nota de material). Pode haver MAIS DE UMA nota (uma por página). "
"Para CADA nota extraia em JSON:\n"
"- ticket: número do chamado, no campo 'Observação' (aparece como 'TICKET', 'TICKER' ou '#' seguido de dígitos). Só os dígitos; se não houver, null.\n"
"- nota_numero: o 'Nº do Documento' da nota (só dígitos).\n"
"- data_nota: a data de emissão ('Dt. Emis') no formato DD/MM/AAAA.\n"
"- itens: linhas da tabela de produtos. Para cada item: descricao (nome do produto SEM o código numérico inicial e sem ' - '), "
"quant (número), unid (coluna 'Embalagem': KG, UN, M...), valor_unit (o 'Preço Unitário', número). Inclua 'SERVICO DE ENTREGA' se existir.\n"
"Responda só JSON: {\"notas\":[{\"ticket\":\"126486\",\"nota_numero\":\"18747\",\"data_nota\":\"03/08/2026\",\"itens\":[{\"descricao\":\"...\",\"quant\":1.0,\"unid\":\"UN\",\"valor_unit\":1.70}]}]}. Use ponto decimal."
)
def _ler_notas_gemini(file_bytes, mime="application/pdf"):
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model=genai.GenerativeModel(GEMINI_MODEL)
    r=model.generate_content([{"mime_type":mime,"data":file_bytes},_ORC_PROMPT],
        generation_config={"response_mime_type":"application/json","temperature":0})
    try: return (json.loads(r.text) or {}).get("notas") or []
    except Exception: return []

def _pasta_manut(access, n):
    try:
        for e in dropbox_rateio.listar_entradas(access, MANUT_BASE):
            if e["dir"] and re.match(rf"^\s*{n}\s*-", e["name"]):
                return f"{MANUT_BASE}/{e['name']}"
    except Exception: pass
    return None

def _loja_do_ticket(ticket):
    q=urllib.parse.urlencode({"numero":f"eq.{ticket}","select":"unidade,aba","limit":"1"})
    ch=_sb_json(f"{SB_URL}/rest/v1/chamados?{q}",SB_KEY) or []
    if not ch: return None
    unidade=ch[0].get("unidade") or ""
    m=re.search(r"LOJA\s*0*(\d{1,3})",unidade,re.I) or re.search(r"\b0*(\d{1,3})\b",unidade)
    lj=None
    if m: lj=(_sb_json(f"{SB_URL}/rest/v1/lojas?numero=eq.{int(m.group(1))}&limit=1",SB_KEY) or [None])[0]
    if not lj and unidade:
        alvo=re.sub(r"^LOJA\s*\d*\s*-?\s*","",unidade,flags=re.I).strip()
        if alvo: lj=(_sb_json(f"{SB_URL}/rest/v1/lojas?nome=ilike.*{urllib.parse.quote(alvo[:14])}*&limit=1",SB_KEY) or [None])[0]
    return {"aba":ch[0].get("aba"),"unidade":unidade,"loja":lj}

def _slug_loja(loja, unidade):
    num=loja.get("numero") if loja else None
    nome=(loja.get("nome") if loja else None) or re.sub(r"^LOJA\s*\d*\s*-?\s*","",unidade or "",flags=re.I).strip() or "SEM_LOJA"
    slug=re.sub(r"\s+","_",nome.strip())
    if num is None: return slug
    return (f"{num:02d}" if num<100 else str(num))+"_"+slug

def _split_pdf(pdf_bytes):
    from pypdf import PdfReader, PdfWriter
    out=[]
    try:
        r=PdfReader(io.BytesIO(pdf_bytes))
        for pg in r.pages:
            w=PdfWriter(); w.add_page(pg); b=io.BytesIO(); w.write(b); out.append(b.getvalue())
    except Exception: return [pdf_bytes]
    return out or [pdf_bytes]

def _orc_registra(row):
    req=urllib.request.Request(f"{SB_URL}/rest/v1/notas_orcamento",
        data=json.dumps([row],ensure_ascii=False).encode(),method="POST",
        headers={"apikey":SB_KEY,"authorization":f"Bearer {SB_KEY}","content-type":"application/json","prefer":"return=minimal"})
    try: urllib.request.urlopen(req,timeout=30)
    except urllib.error.HTTPError as e: print("notas_orcamento erro:",e.read().decode()[:200],flush=True)

@app.post("/orc/listar")
def orc_listar(request: Request):
    from fastapi import HTTPException
    exige(request,"CONFERIR_LISTA_ORCAMENTOS")
    if not dropbox_rateio.ativo(): raise HTTPException(500,"Dropbox não configurado")
    access=dropbox_rateio.obter_token()
    arqs=[a for a in dropbox_rateio.listar(access,ORC_NOTAS) if a.lower().endswith((".pdf",".jpg",".jpeg",".png"))]
    return {"pasta":ORC_NOTAS,"total":len(arqs),"arquivos":sorted(arqs)}

@app.post("/orc/gerar")
async def orc_gerar(request: Request):
    from fastapi import HTTPException
    u,p=exige(request,"GERAR_ORCAMENTOS")
    body={}
    try: body=await request.json()
    except Exception: pass
    previa=bool(body.get("previa"))
    if not GEMINI_API_KEY: raise HTTPException(500,"GEMINI_API_KEY não configurada no Render")
    access=dropbox_rateio.obter_token()
    P6=_pasta_manut(access,6); P7=_pasta_manut(access,7); P8=_pasta_manut(access,8)
    P1=_pasta_manut(access,1); P9=_pasta_manut(access,9); P10=_pasta_manut(access,10)
    arqs=[a for a in dropbox_rateio.listar(access,ORC_NOTAS) if a.lower().endswith((".pdf",".jpg",".jpeg",".png"))]
    def _mime(nm):
        nl=nm.lower(); return "application/pdf" if nl.endswith(".pdf") else ("image/png" if nl.endswith(".png") else "image/jpeg")
    res=[]
    for nome in sorted(arqs):
        ext=os.path.splitext(nome)[1] or ".pdf"; is_pdf=nome.lower().endswith(".pdf")
        fb=dropbox_rateio.baixar(access,f"{ORC_NOTAS}/{nome}")
        if not fb: res.append({"arquivo":nome,"status":"erro","motivo":"não baixou"}); continue
        try: notas=_ler_notas_gemini(fb,_mime(nome))
        except Exception as e:
            res.append({"arquivo":nome,"status":"erro","motivo":f"gemini: {str(e)[:120]}"}); continue
        if not notas:
            info={"arquivo":nome,"status":"pendente","motivo":"não identifiquei nota","destino":"6"}
            if not previa and P6: dropbox_rateio.mover(access,f"{ORC_NOTAS}/{nome}",f"{P6}/{nome}")
            res.append(info); continue
        pages=_split_pdf(fb) if (is_pdf and len(notas)>1) else None
        usou_source=False
        for i,nt in enumerate(notas):
            page=pages[i] if (pages and i<len(pages)) else fb
            ticket=_num_limpo(nt.get("ticket")); nota_num=_num_limpo(nt.get("nota_numero")) or "SN"
            itens=nt.get("itens") or []
            valor_nota=0.0
            for it in itens:
                try: valor_nota+=float(it.get("valor_unit") or 0)*float(it.get("quant") or 0)
                except Exception: pass
            valor_nota=round(valor_nota,2); valor_orc=round(valor_nota*1.20,2)
            info={"arquivo":nome,"ticket":ticket or None,"nota":nota_num,"itens":len(itens),"valor_nota":valor_nota,"valor_orcamento":valor_orc}
            def _mv_nota(destino_folder, destino_nome):
                nonlocal usou_source
                if previa or not destino_folder: return
                if pages is None:
                    dropbox_rateio.mover(access,f"{ORC_NOTAS}/{nome}",f"{destino_folder}/{destino_nome}"); usou_source=True
                else:
                    dropbox_rateio.subir_bytes(access,page,f"{destino_folder}/{destino_nome}",overwrite=True)
            if not ticket:
                info.update(status="pendente",motivo="sem ticket",destino="6")
                _mv_nota(P6, nome if pages is None else f"{os.path.splitext(nome)[0]}_p{i+1}{ext}")
                if not previa: _orc_registra({"nota_numero":nota_num if nota_num!="SN" else None,"ticket":None,"status":"sem_ticket","valor_nota":valor_nota,"itens":itens,"criado_por":u["id"]})
                res.append(info); continue
            lj=_loja_do_ticket(ticket)
            if not lj:
                info.update(status="pendente",motivo="ticket não encontrado nos chamados",destino="7")
                _mv_nota(P7, nome if pages is None else f"TICKET_{ticket}_NOTA_{nota_num}{ext}")
                if not previa: _orc_registra({"nota_numero":nota_num if nota_num!="SN" else None,"ticket":ticket,"status":"ticket_nao_associado","valor_nota":valor_nota,"itens":itens,"criado_por":u["id"]})
                res.append(info); continue
            loja=lj.get("loja") or {}
            loja_nome=(loja.get("nome") if loja else None) or re.sub(r"^LOJA\s*\d*\s*-?\s*","",lj.get("unidade") or "",flags=re.I).strip() or "—"
            extrap=valor_nota>ORC_EXTRAPOLA
            slug=_slug_loja(loja,lj.get("unidade"))
            mes=_mes_da_data(nt.get("data_nota"))
            info.update(status="ok",loja=loja_nome,loja_numero=(loja.get("numero") if loja else None),extrapolado=extrap,mes=mes)
            if not previa:
                # dados do orçamento
                hoje=datetime.date.today().strftime("%d/%m/%Y")
                itens_orc=[{"descricao":it.get("descricao"),"quant":float(it.get("quant") or 0),"unid":it.get("unid") or "UN","valor_unit":float(it.get("valor_unit") or 0)} for it in itens]
                dados={"num":ticket,"revisao":1,"data":hoje,"loja_nome":loja_nome,
                    "prestador":{"nome":"Frota Macedo Engenharia LTDA","cnpj":"27.363.223/0001-70","forma":"Boleto 30 dias"},
                    "tomador":{"nome":f"Mercadinhos São Luiz — {loja_nome.title()}","cnpj":(loja.get("cnpj") if loja else None),
                               "endereco":(loja.get("endereco") if loja else None),
                               "cidade":((loja.get("cidade") if loja else None) or "")+(" - CE" if (loja.get("cidade") if loja else None) else "")},
                    "itens":itens_orc}
                base_nome=f"{slug}_{ticket}_NOTA_{nota_num}"
                doc_bytes=gera_orcamento_docx(dados)
                arq_pdf=arq_doc=None
                try:
                    if extrap:
                        if P9: dropbox_rateio.subir_bytes(access,doc_bytes,f"{P9}/{base_nome}.docx",overwrite=True); arq_doc=f"9/{base_nome}.docx"
                    else:
                        pdf_bytes=gera_orcamento_pdf(dados)
                        if P1: dropbox_rateio.subir_bytes(access,pdf_bytes,f"{P1}/{base_nome}.pdf",overwrite=True)
                        if P10:
                            dropbox_rateio.criar_pasta(access,f"{P10}/{mes}"); dropbox_rateio.criar_pasta(access,f"{P10}/{mes}/{slug}")
                            dropbox_rateio.subir_bytes(access,pdf_bytes,f"{P10}/{mes}/{slug}/{base_nome}.pdf",overwrite=True)
                            dropbox_rateio.subir_bytes(access,doc_bytes,f"{P10}/{mes}/{slug}/{base_nome}.docx",overwrite=True)
                            arq_pdf=f"10/{mes}/{slug}/{base_nome}.pdf"; arq_doc=f"10/{mes}/{slug}/{base_nome}.docx"
                except Exception as e: info.update(motivo=f"salvar: {str(e)[:90]}")
                # nota renomeada -> pasta 8 / <aba>
                sub="INSTALACOES" if (lj.get("aba") or "").upper().startswith("INST") else ("CIVIL" if (lj.get("aba") or "").upper().startswith("CIV") else "SEM CLASSIFICACAO")
                arq_nota=None
                if P8:
                    dropbox_rateio.criar_pasta(access,f"{P8}/{sub}")
                    _mv_nota(f"{P8}/{sub}", f"TICKET_{ticket}_NOTA_{nota_num}{ext}")
                    arq_nota=f"8/{sub}/TICKET_{ticket}_NOTA_{nota_num}{ext}"
                _orc_registra({"nota_numero":nota_num if nota_num!="SN" else None,"ticket":ticket,
                    "loja_numero":(loja.get("numero") if loja else None),"loja_nome":loja_nome,"aba":lj.get("aba"),
                    "valor_nota":valor_nota,"valor_orcamento":valor_orc,"status":"gerado","extrapolado":extrap,
                    "itens":itens_orc,"data_nota":_data_iso(nt.get("data_nota")),"mes_ref":mes,
                    "arquivo_nota":arq_nota,"arquivo_pdf":arq_pdf,"arquivo_doc":arq_doc,"criado_por":u["id"]})
            res.append(info)
        # se dividiu em páginas, remove o arquivo-fonte da pasta 0 (páginas já foram espalhadas)
        if not previa and pages is not None and not usou_source:
            try: dropbox_rateio.apagar(access,f"{ORC_NOTAS}/{nome}")
            except Exception: pass
    ok=sum(1 for r in res if r.get("status")=="ok")
    if not previa: log_frotahub(u["id"],p["papel"],"GERAR_ORCAMENTOS","GEROU",f"{ok}/{len(res)}")
    return {"previa":previa,"resultados":res,"gerados":ok,"total":len(res)}

def _basic_auth(app, user, pw):
    """Protege TODAS as rotas com usuário/senha (HTTP Basic), na camada ASGI."""
    import base64, secrets
    async def wrapped(scope, receive, send):
        # rotas do FrotaHub têm autenticação própria (token Supabase) — não pedir Basic
        _path = scope.get("path", "") or ""
        if scope["type"] == "http" and (_path.startswith("/pco") or _path.startswith("/api")
                                        or _path.startswith("/notas") or _path.startswith("/orc") or _path.startswith("/migrar")
                                        or _path.startswith("/desfazer")
                                        or scope.get("method") == "OPTIONS"):
            await app(scope, receive, send); return
        if scope["type"] in ("http", "websocket"):
            hdrs = dict(scope.get("headers") or [])
            auth = hdrs.get(b"authorization", b"").decode()
            ok = False
            if auth.startswith("Basic "):
                try:
                    usr, _, pwd = base64.b64decode(auth[6:]).decode("utf-8", "ignore").partition(":")
                    ok = secrets.compare_digest(usr, user) and secrets.compare_digest(pwd, pw)
                except Exception:
                    ok = False
            if not ok:
                if scope["type"] == "http":
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"www-authenticate", b'Basic realm="Motor"'),
                                            (b"content-type", b"text/plain; charset=utf-8")]})
                    await send({"type": "http.response.body", "body": "Autenticação necessária".encode()})
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await app(scope, receive, send)
    return wrapped

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))    # Render/Cloud injeta PORT
    u, p = os.environ.get("APP_USER", ""), os.environ.get("APP_PASS", "")
    import uvicorn
    asgi = _basic_auth(app, u, p) if (u and p) else app
    uvicorn.run(asgi, host="0.0.0.0", port=port)
