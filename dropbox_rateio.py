# -*- coding: utf-8 -*-
"""
RATEIO NO DROPBOX ONLINE
Move/coloca cada nota lida na pasta certa do Dropbox, pela API (sem PC, sem sync local).

Pastas de destino (dentro de DROPBOX_BASE), iguais às da skill:
  NOTAS INCLUIDAS ORCAMENTO/INSTALACOES   -> nota virou orçamento, chamado da aba Instalações
  NOTAS INCLUIDAS ORCAMENTO/CIVIL         -> nota virou orçamento, chamado da aba Civil
  SEM TICKET                              -> nota sem número de ticket
  TICKET NAO ASSOCIADO                    -> tem ticket, mas não achou o chamado no Supabase
  ARQUIVOS RESIDUAIS                      -> arquivo que não deu para ler

Autenticação (Settings -> Variables and secrets do Space):
  Recomendado (não expira):
    DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
  Simples (token expira em ~4h, só para teste):
    DROPBOX_TOKEN
  Caminho base no Dropbox (confirmar!):
    DROPBOX_BASE   ex.: /AUTOMAÇÃO ADMINISTRATIVO/3 - ORÇAMENTOS/RATEIO
"""
import os, json, urllib.parse, urllib.request

APP_KEY   = os.environ.get("DROPBOX_APP_KEY", "")
APP_SECRET= os.environ.get("DROPBOX_APP_SECRET", "")
REFRESH   = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
TOKEN     = os.environ.get("DROPBOX_TOKEN", "")
BASE      = os.environ.get("DROPBOX_BASE", "/AUTOMACAO ADMINISTRATIVO/3 - ORCAMENTOS/RATEIO").rstrip("/")

PASTAS = {
    "INSTALACOES":     "NOTAS INCLUIDAS ORCAMENTO/INSTALACOES",
    "CIVIL":           "NOTAS INCLUIDAS ORCAMENTO/CIVIL",
    "SEM TICKET":      "SEM TICKET",
    "NAO ASSOCIADO":   "TICKET NAO ASSOCIADO",
    "RESIDUAL":        "ARQUIVOS RESIDUAIS",
}

def ativo():
    return bool((TOKEN or (APP_KEY and APP_SECRET and REFRESH)))

def _access_token():
    """Token de acesso: usa o refresh token (renova) ou o DROPBOX_TOKEN direto."""
    if APP_KEY and APP_SECRET and REFRESH:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": REFRESH,
            "client_id": APP_KEY, "client_secret": APP_SECRET,
        }).encode()
        req = urllib.request.Request("https://api.dropbox.com/oauth2/token", data=data)
        r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        return r.get("access_token")
    return TOKEN

def _upload(access, local_path, dropbox_path):
    """Sobe 1 arquivo (add, sem sobrescrever: autorename se já existir)."""
    with open(local_path, "rb") as fh:
        conteudo = fh.read()
    arg = {"path": dropbox_path, "mode": "add", "autorename": True, "mute": True}
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/upload", data=conteudo,
        headers={
            "Authorization": f"Bearer {access}",
            "Dropbox-API-Arg": json.dumps(arg),
            "Content-Type": "application/octet-stream",
        })
    urllib.request.urlopen(req, timeout=120).read()

def _dest(categoria):
    sub = PASTAS.get(categoria, PASTAS["RESIDUAL"])
    return f"{BASE}/{sub}"

def ratear(itens):
    """
    itens: lista de dicts {"local": <caminho do arquivo>, "categoria": <chave de PASTAS>,
                           "nome_destino": <opcional: nome do arquivo no Dropbox>}
    Retorna (ok:int, erros:list[str]).
    """
    if not ativo():
        return 0, ["Dropbox não configurado (sem token)."]
    try:
        access = _access_token()
    except Exception as e:
        return 0, [f"Falha ao autenticar no Dropbox: {e}"]
    ok, erros = 0, []
    for it in itens:
        local = it.get("local")
        if not (local and os.path.exists(local)):
            continue
        nome = it.get("nome_destino") or os.path.basename(local)
        destino = f"{_dest(it.get('categoria','RESIDUAL'))}/{nome}"
        try:
            _upload(access, local, destino)
            ok += 1
        except Exception as e:
            erros.append(f"{nome}: {e}")
    return ok, erros
