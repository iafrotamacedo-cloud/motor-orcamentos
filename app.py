# -*- coding: utf-8 -*-
"""
MOTOR DE ORÇAMENTOS — Frota Macedo (100% online, Hugging Face Spaces)
Operador sobe notas/DAVs (+ opcional planilha de controle) -> OK -> baixa um ZIP com:
  - orçamentos (Word + PDF) por Mês/Loja
  - planilha de controle mensal atualizada
  - planilha de correção (notas SEM TICKET / TICKET NÃO ASSOCIADO)

Leitura das notas: Google Gemini (visão).  Ticket->loja: tabela `chamados` no Supabase.
Regras (reaproveitadas dos scripts): +20% no unitário, rateio de entrega, 1 orçamento por ticket.

Segredos (Settings -> Variables and secrets, no Space):
  GEMINI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
  (opcionais) GEMINI_MODEL, FAT_NOME, FAT_CNPJ
"""
import os, re, json, shutil, subprocess, tempfile, datetime, zipfile, unicodedata, urllib.parse, urllib.request, urllib.error
import google.generativeai as genai
import dropbox_rateio

BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(BASE, "scripts")
ASSETS = os.path.join(BASE, "assets")
LOGO = os.path.join(ASSETS, "logo_frota.jpg")
CAD = json.load(open(os.path.join(ASSETS, "cadastro_lojas.json"), encoding="utf-8"))

GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GROQ_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
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
    from PIL import Image
    img = Image.open(png_path)
    model = genai.GenerativeModel(GEMINI_MODEL)
    r = model.generate_content([PROMPT, img],
        generation_config={"temperature": 0, "response_mime_type": "application/json"})
    return _parse_json(r.text)

def groq_le(png_path):
    import base64
    b64 = base64.b64encode(open(png_path, "rb").read()).decode()
    payload = {
        "model": GROQ_MODEL, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
    }
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"})
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
    novas_notas = set()
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
            # dedup por número da nota: mesma nota já feita no mês (ou repetida no lote) -> ignora
            if numdoc and (numdoc in notas_feitas or numdoc in novas_notas):
                duplicadas += 1; continue
            if numdoc: novas_notas.add(numdoc)
            reg = {"nota": numdoc, "fornecedor": nota.get("fornecedor") or "", "ticket": ticket, "loja": ""}
            pg = {"src": path, "page": idx, "is_img": is_img, "ticket": ticket, "num": numdoc}
            if not ticket:
                diag.append({"forn": nota.get("fornecedor") or "?", "obs": str(nota.get("obs") or "")[:140], "err": nota.get("_erro")})
                sem_ticket.append({**reg, "status": "SEM TICKET"}); pg["cat"] = "SEM TICKET"; paginas.append(pg); continue
            ch = busca_chamado(ticket)
            if not ch:
                nao_assoc.append({**reg, "status": "TICKET NÃO ASSOCIADO"}); pg["cat"] = "NAO ASSOCIADO"; paginas.append(pg); continue
            pg["cat"] = (ch.get("aba") or "CIVIL").upper(); paginas.append(pg)
            g = por_ticket.setdefault(ticket, {"chamado": ch, "itens": [], "forma": nota.get("forma_pagamento")})
            g["itens"].extend(itens)
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
    for x in diag[:5]:
        det = x.get("err") and f" | ERRO leitura: {x['err']}" or ""
        msg.append(f"   🔎 obs lida: «{x['obs']}»{det}")
    if nao_assoc:  msg.append(f"⚠️ {len(nao_assoc)} TICKET NÃO ASSOCIADO (planilha de correção atualizada).")
    if ctrl_ok:    msg.append("🧾 Planilha de controle do mês atualizada no Dropbox — baixe abaixo p/ enviar ao cliente.")
    errs = erros_dbx + erros_r
    if errs: msg.append("⚠️ Erros no Dropbox: " + "; ".join(errs[:4]))
    return (ctrl if ctrl_ok else None), "\n".join(msg)

# ---------- interface (FastAPI puro — sem Gradio) ----------
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse

app = FastAPI(title="Motor de Orçamentos — Frota Macedo")
RESULTS = {}   # token -> caminho do zip

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
 <div id="msg"></div>
</div>
<script>
const f=document.getElementById('f'), b=document.getElementById('b'), msg=document.getElementById('msg');
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

@app.get("/baixar")
def baixar(t: str):
    path = RESULTS.get(t)
    if not path or not os.path.exists(path):
        return PlainTextResponse("Arquivo expirado — gere novamente.", status_code=404)
    return FileResponse(path, filename=os.path.basename(path),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def _basic_auth(app, user, pw):
    """Protege TODAS as rotas com usuário/senha (HTTP Basic), na camada ASGI."""
    import base64, secrets
    async def wrapped(scope, receive, send):
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
