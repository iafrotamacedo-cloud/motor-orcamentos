# -*- coding: utf-8 -*-
# =====================================================================
#  CONTADOR DE REVISÕES DESTE app.py: 55
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
def api_ping(): return {"ok": True, "motor": "frotahub", "rev": 55}

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
    linhas = [f"Faturamento: {cc_cnpj}" + (f" — {centro}" if centro else ""),
              f"Fornecedor: {forn}  CNPJ: {forn_cnpj}"] + ([f"Endereço: {endereco}"] if endereco else [])
    corrig = _pdf_carimbo_correcao(orig_pdf, linhas)
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
    val = f"(numero.ilike.*{q}*,fornecedor.ilike.*{q}*,centro_custo.ilike.*{q}*)"
    url = (f"{SB_URL}/rest/v1/v_ocs?pco_status=eq.enviado&or={urllib.parse.quote(val)}"
           f"&select=id,numero,data_oc,centro_custo,fornecedor,valor,pco_enviado_em,qtd_notas,valor_notas,fisica_ok,protocolo_ok,ciclo_fechado&order=numero&limit=40")
    try: rows = _sb_json(url, SB_KEY) or []
    except Exception as e: raise HTTPException(500, f"procurar: {e}")
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

def _basic_auth(app, user, pw):
    """Protege TODAS as rotas com usuário/senha (HTTP Basic), na camada ASGI."""
    import base64, secrets
    async def wrapped(scope, receive, send):
        # rotas do FrotaHub têm autenticação própria (token Supabase) — não pedir Basic
        _path = scope.get("path", "") or ""
        if scope["type"] == "http" and (_path.startswith("/pco") or _path.startswith("/api")
                                        or _path.startswith("/notas") or _path.startswith("/migrar")
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
