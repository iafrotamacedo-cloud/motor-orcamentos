# -*- coding: utf-8 -*-
"""
DROPBOX ONLINE — rateio de notas + gravação de orçamentos e planilhas, pela API.

Pastas (dentro de DROPBOX_BASE), iguais às da skill:
  NOTAS INCLUIDAS ORCAMENTO/INSTALACOES | CIVIL   -> nota que virou orçamento
  SEM TICKET | TICKET NAO ASSOCIADO                -> notas de correção
  ARQUIVOS RESIDUAIS (NÃO MEXER)                   -> arquivo ilegível
  ORCAMENTOS MONTADOS/<mês>/<loja>                 -> PDFs dos orçamentos + planilha de controle
  1 - ORÇAMENTOS NÃO LANÇADOS                      -> cópia (PDF) p/ o robô do Trílogo

Autenticação (Variables and secrets do serviço):
  DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN   (recomendado, não expira)
  DROPBOX_TOKEN                                                (simples, expira ~4h)
  DROPBOX_BASE   ex.: /AUTOMACAO MANUTENCAO
"""
import os, json, urllib.parse, urllib.request, urllib.error

APP_KEY   = os.environ.get("DROPBOX_APP_KEY", "")
APP_SECRET= os.environ.get("DROPBOX_APP_SECRET", "")
REFRESH   = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
TOKEN     = os.environ.get("DROPBOX_TOKEN", "")
BASE      = os.environ.get("DROPBOX_BASE", "/AUTOMACAO MANUTENCAO").rstrip("/")

ORCAMENTOS   = os.environ.get("DROPBOX_ORCAMENTOS", "ORCAMENTOS MONTADOS")
NAO_LANCADOS = os.environ.get("DROPBOX_NAO_LANCADOS", "1 - ORÇAMENTOS NÃO LANÇADOS")

PASTAS = {
    "INSTALACOES":     "NOTAS INCLUIDAS ORCAMENTO/INSTALACOES",
    "CIVIL":           "NOTAS INCLUIDAS ORCAMENTO/CIVIL",
    "SEM TICKET":      "SEM TICKET",
    "NAO ASSOCIADO":   "TICKET NAO ASSOCIADO",
    "RESIDUAL":        "ARQUIVOS RESIDUAIS (NÃO MEXER)",
}

def ativo():
    return bool(TOKEN or (APP_KEY and APP_SECRET and REFRESH))

def obter_token():
    """Token de acesso (renova pelo refresh token) ou DROPBOX_TOKEN direto."""
    if APP_KEY and APP_SECRET and REFRESH:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": REFRESH,
            "client_id": APP_KEY, "client_secret": APP_SECRET,
        }).encode()
        req = urllib.request.Request("https://api.dropbox.com/oauth2/token", data=data)
        r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        return r.get("access_token")
    return TOKEN

def baixar(access, dropbox_path):
    """Baixa um arquivo do Dropbox. Retorna bytes, ou None se não existir."""
    req = urllib.request.Request("https://content.dropboxapi.com/2/files/download",
        data=b"", headers={"Authorization": f"Bearer {access}",
                           "Dropbox-API-Arg": json.dumps({"path": dropbox_path})})
    try:
        return urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):   # not_found
            return None
        raise

def subir_bytes(access, data, dropbox_path, overwrite=False):
    arg = {"path": dropbox_path, "mode": ("overwrite" if overwrite else "add"),
           "autorename": (not overwrite), "mute": True}
    req = urllib.request.Request("https://content.dropboxapi.com/2/files/upload",
        data=data, headers={"Authorization": f"Bearer {access}",
                            "Dropbox-API-Arg": json.dumps(arg),
                            "Content-Type": "application/octet-stream"})
    urllib.request.urlopen(req, timeout=180).read()

def subir(access, local_path, dropbox_path, overwrite=False):
    with open(local_path, "rb") as fh:
        subir_bytes(access, fh.read(), dropbox_path, overwrite)

def _dest(categoria):
    return f"{BASE}/{PASTAS.get(categoria, PASTAS['RESIDUAL'])}"

def ratear(access, itens):
    """itens: [{"local":caminho, "categoria":chave, "nome_destino":opcional}] -> (ok, erros)."""
    ok, erros = 0, []
    for it in itens:
        local = it.get("local")
        if not (local and os.path.exists(local)):
            continue
        nome = it.get("nome_destino") or os.path.basename(local)
        destino = f"{_dest(it.get('categoria', 'RESIDUAL'))}/{nome}"
        try:
            subir(access, local, destino, overwrite=False); ok += 1
        except Exception as e:
            erros.append(f"{nome}: {e}")
    return ok, erros
