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
import os, re, json, shutil, subprocess, tempfile, datetime, zipfile, unicodedata, urllib.parse, urllib.request
os.environ.setdefault("GRADIO_SSR_MODE", "False")   # sem SSR -> não precisa de Node
import gradio as gr
import google.generativeai as genai
import dropbox_rateio

BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(BASE, "scripts")
ASSETS = os.path.join(BASE, "assets")
LOGO = os.path.join(ASSETS, "logo_frota.jpg")
CAD = json.load(open(os.path.join(ASSETS, "cadastro_lojas.json"), encoding="utf-8"))

GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
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
Extraia SOMENTE o que estiver na nota e responda em JSON puro (sem texto fora do JSON):
{
 "ticket": "<número do ticket/chamado, 5-6 dígitos, aparece como #NNNNNN, TICKET NNNNNN, TICKTE/ NNNNNN, ORÇAMENTO NNNNNN, em Observação/Dados adicionais; se não houver, null>",
 "num_documento": "<Nº do documento/nota; se não houver, null>",
 "fornecedor": "<razão social do emitente>",
 "forma_pagamento": "<ex.: Boleto 30 dias, se aparecer; senão null>",
 "itens": [
   {"desc":"<descrição do item>", "quant":<número>, "unid":"<UN/PC/SV/…>", "unit":<valor UNITÁRIO BRUTO, sem imposto/desconto>}
 ]
}
Regras: NÃO aplique nenhum acréscimo/desconto — use o valor unitário BRUTO. Inclua item de ENTREGA/FRETE se houver (desc contendo 'ENTREGA' ou 'FRETE'). quant e unit são números (ponto decimal)."""

def gemini_le(png_path):
    from PIL import Image
    img = Image.open(png_path)
    model = genai.GenerativeModel(GEMINI_MODEL)
    r = model.generate_content([PROMPT, img],
        generation_config={"temperature": 0, "response_mime_type": "application/json"})
    txt = (r.text or "").strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {"ticket": None, "itens": []}

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
               "cnpj": (e.get("cnpj") or "—"), "endereco": e.get("endereco") or "—", "cidade": ""}
    obra = f"{norm(chamado.get('descricao'))}  #{ticket}"
    dados = {"numero_ticket": str(ticket), "revisao": 1, "data": data_str, "obra": obra,
             "faturamento": {"nome": FAT_NOME, "cnpj": FAT_CNPJ,
                             "forma_pagamento": forma_pag or "", "data_faturamento": data_str},
             "cliente": cliente, "itens": prep["itens"],
             "total_geral": prep["total_geral"], "extenso": prep["extenso"]}
    dj = os.path.join(workdir, f"dados_{ticket}.json")
    json.dump(dados, open(dj, "w"), ensure_ascii=False)
    # pasta Mês/Loja
    hoje = datetime.date.today()
    pasta_mes = os.path.join(outdir, "ORCAMENTOS MONTADOS", f"{MESES[hoje.month-1]} {hoje.year}")
    pasta_loja = os.path.join(pasta_mes, f"{num}_{slug(nome_loja)}")
    os.makedirs(pasta_loja, exist_ok=True)
    base = f"{num}_{nome_loja}_{ticket}"
    pdf = os.path.join(pasta_loja, base + ".pdf")
    subprocess.run(["python3", os.path.join(SCRIPTS, "gerar_pdf.py"), dj, pdf, LOGO],
                   capture_output=True)
    # planilha mensal
    xlsx = os.path.join(pasta_mes, f"ORÇAMENTOS MONTADOS - {MESES[hoje.month-1]} {hoje.year}.xlsx")
    if os.path.exists(pdf):
        subprocess.run(["python3", os.path.join(SCRIPTS, "atualizar_planilha_mensal.py"),
                        "--xlsx", xlsx, "--ticket", str(ticket), "--loja", nome_loja,
                        "--pdf", pdf, "--data", data_str], capture_output=True)
    return dict(ticket=ticket, loja=f"{num} - {nome_loja}", total=prep["total_geral"],
                pdf_ok=os.path.exists(pdf))

# ---------- orquestração ----------
def processar(arquivos, planilha_controle):
    if not GEMINI_KEY:
        return None, "⚠️ Falta configurar o segredo GEMINI_API_KEY no Space."
    if not (SB_URL and SB_KEY):
        return None, "⚠️ Faltam os segredos SUPABASE_URL / SUPABASE_SERVICE_KEY."
    work = tempfile.mkdtemp(prefix="orc_")
    out = os.path.join(work, "SAIDA"); os.makedirs(out, exist_ok=True)
    data_str = datetime.date.today().strftime("%d/%m/%Y")

    # se enviaram a planilha de controle, ela entra na pasta do mês para ser atualizada
    hoje = datetime.date.today()
    pasta_mes = os.path.join(out, "ORCAMENTOS MONTADOS", f"{MESES[hoje.month-1]} {hoje.year}")
    os.makedirs(pasta_mes, exist_ok=True)
    if planilha_controle:
        shutil.copy(planilha_controle, os.path.join(pasta_mes, f"ORÇAMENTOS MONTADOS - {MESES[hoje.month-1]} {hoje.year}.xlsx"))

    # 1) lê todas as notas (páginas) com o Gemini e agrupa por ticket
    por_ticket = {}   # ticket -> {"chamado":..., "itens":[...], "forma":...}
    sem_ticket, nao_assoc = [], []
    rateio = {}       # caminho do arquivo -> {"cat","ticket","num"} (p/ o Dropbox)
    PRIO = {"RESIDUAL": 0, "SEM TICKET": 1, "NAO ASSOCIADO": 2, "CIVIL": 3, "INSTALACOES": 3}
    def marca(path, cat, ticket="", num=""):
        cur = rateio.setdefault(path, {"cat": "RESIDUAL", "ticket": "", "num": ""})
        if PRIO.get(cat, 0) >= PRIO.get(cur["cat"], 0):
            cur["cat"] = cat
        if ticket and not cur["ticket"]: cur["ticket"] = ticket
        if num and not cur["num"]: cur["num"] = num
    for f in (arquivos or []):
        path = f.name if hasattr(f, "name") else f
        rateio.setdefault(path, {"cat": "RESIDUAL", "ticket": "", "num": ""})
        for idx, png in paginas_imagens(path, work):
            try:
                nota = gemini_le(png)
            except Exception as e:
                nota = {"ticket": None, "itens": [], "fornecedor": None}
            ticket = re.sub(r"\D", "", str(nota.get("ticket") or ""))
            itens = nota.get("itens") or []
            numdoc = str(nota.get("num_documento") or "")
            reg = {"nota": numdoc, "fornecedor": nota.get("fornecedor") or "",
                   "ticket": ticket, "loja": ""}
            if not ticket:
                sem_ticket.append({**reg, "status": "SEM TICKET"}); marca(path, "SEM TICKET", num=numdoc); continue
            ch = busca_chamado(ticket)
            if not ch:
                nao_assoc.append({**reg, "status": "TICKET NÃO ASSOCIADO"}); marca(path, "NAO ASSOCIADO", ticket=ticket, num=numdoc); continue
            marca(path, (ch.get("aba") or "CIVIL").upper(), ticket=ticket, num=numdoc)
            g = por_ticket.setdefault(ticket, {"chamado": ch, "itens": [], "forma": nota.get("forma_pagamento")})
            g["itens"].extend(itens)
            if nota.get("forma_pagamento") and not g["forma"]: g["forma"] = nota.get("forma_pagamento")

    # 2) gera 1 orçamento por ticket
    feitos = []
    for ticket, g in por_ticket.items():
        if not g["itens"]: continue
        try:
            feitos.append(gera_orcamento(ticket, g["chamado"], g["itens"], work, out, data_str, g["forma"]))
        except Exception as e:
            nao_assoc.append({"nota": "", "fornecedor": "", "status": f"ERRO: {e}", "ticket": ticket, "loja": ""})

    # 3) planilha de correção (SEM TICKET + TICKET NÃO ASSOCIADO)
    pend = sem_ticket + nao_assoc
    if pend:
        corr = os.path.join(out, "NOTAS PARA CORREÇÃO.xlsx")
        inp = os.path.join(work, "pend.json"); json.dump(pend, open(inp, "w"), ensure_ascii=False)
        subprocess.run(["python3", os.path.join(SCRIPTS, "pendentes.py"), "add",
                        "--xlsx", corr, "--input", inp], capture_output=True)

    # 4) zip
    zip_path = os.path.join(work, f"orcamentos_{hoje.strftime('%Y-%m-%d')}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(out):
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, out))

    # 5) rateio direto no Dropbox online (se configurado)
    dbx_msg = ""
    if dropbox_rateio.ativo():
        def nome_dest(path, info):
            ext = (os.path.splitext(path)[1] or ".pdf")
            num = re.sub(r'[\\/:*?"<>|]', "", str(info.get("num") or "")).strip()
            tk = info.get("ticket") or ""
            if info["cat"] == "SEM TICKET":
                base = f"TICKET SEMTICKET - NOTA {num}" if num else os.path.splitext(os.path.basename(path))[0]
            elif tk:
                base = f"TICKET {tk} - NOTA {num}" if num else f"TICKET {tk}"
            else:
                base = os.path.splitext(os.path.basename(path))[0]
            return base + ext
        itens_dbx = [{"local": p, "categoria": info["cat"], "nome_destino": nome_dest(p, info)}
                     for p, info in rateio.items()]
        ok, erros = dropbox_rateio.ratear(itens_dbx)
        dbx_msg = f"📁 Dropbox: {ok} arquivo(s) rateado(s)."
        if erros: dbx_msg += f"  ({len(erros)} falha(s): " + "; ".join(erros[:3]) + ")"

    msg = [f"✅ {len(feitos)} orçamento(s) gerado(s)."]
    for d in feitos: msg.append(f"• Ticket {d['ticket']} — {d['loja']} — {d['total']}" + ("" if d['pdf_ok'] else "  (⚠️ PDF falhou)"))
    if sem_ticket: msg.append(f"⚠️ {len(sem_ticket)} nota(s) SEM TICKET (na planilha de correção).")
    if nao_assoc: msg.append(f"⚠️ {len(nao_assoc)} nota(s) com TICKET NÃO ASSOCIADO (na planilha de correção).")
    if dbx_msg: msg.append(dbx_msg)
    return zip_path, "\n".join(msg)

# ---------- interface ----------
with gr.Blocks(title="Motor de Orçamentos — Frota Macedo") as demo:
    gr.Markdown("## Motor de Orçamentos — Frota Macedo\nSuba as **notas/DAVs** (PDF ou imagem) e, se quiser, a **planilha de controle** do mês. Clique em **Gerar** e baixe o ZIP.")
    with gr.Row():
        notas = gr.File(label="Notas / DAVs", file_count="multiple",
                        file_types=[".pdf", ".jpg", ".jpeg", ".png"])
        controle = gr.File(label="Planilha de controle do mês (opcional)", file_types=[".xlsx"])
    btn = gr.Button("Gerar orçamentos", variant="primary")
    saida = gr.File(label="ZIP com os orçamentos")
    status = gr.Textbox(label="Resultado", lines=10)
    btn.click(processar, inputs=[notas, controle], outputs=[saida, status])

def _basic_auth(app, user, pw):
    """Protege TODAS as rotas com usuário/senha (HTTP Basic), na camada ASGI —
    não interfere no streaming do Gradio."""
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
    # Sobe via uvicorn (evita o autocheck de localhost do Gradio atrás de proxy)
    import uvicorn
    from fastapi import FastAPI
    demo.queue()
    asgi = gr.mount_gradio_app(FastAPI(), demo, path="/")
    if u and p:
        asgi = _basic_auth(asgi, u, p)
    uvicorn.run(asgi, host="0.0.0.0", port=port)
