# -*- coding: utf-8 -*-
"""
Cliente mínimo do Supabase Storage (REST) para o FrotaHub.
Sem dependências externas — usa urllib. Todas as funções recebem (base, key, bucket).

base = SUPABASE_URL   (ex.: https://xxxx.supabase.co)
key  = SUPABASE_SERVICE_KEY  (chave de serviço — SOMENTE no servidor/motor)
bucket = nome do bucket (ex.: "frotahub")

Convenção de caminho: "<slot>/<nome>"  (ex.: "orc_nao_lancados/OS123.pdf")
"""
import json, urllib.request, urllib.parse, urllib.error

def _req(method, url, key, data=None, headers=None, timeout=60):
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    if headers: h.update(headers)
    rq = urllib.request.Request(url, data=data, method=method, headers=h)
    return urllib.request.urlopen(rq, timeout=timeout)

def _ctype(nome):
    n = nome.lower()
    if n.endswith(".pdf"): return "application/pdf"
    if n.endswith((".jpg", ".jpeg")): return "image/jpeg"
    if n.endswith(".png"): return "image/png"
    if n.endswith(".xlsx"): return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if n.endswith(".docx"): return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if n.endswith(".zip"): return "application/zip"
    return "application/octet-stream"

def upload(base, key, bucket, path, data, content_type=None, upsert=True):
    """Sobe bytes para <bucket>/<path>. Retorna True/False."""
    url = f"{base}/storage/v1/object/{bucket}/{urllib.parse.quote(path)}"
    hd = {"Content-Type": content_type or _ctype(path), "x-upsert": "true" if upsert else "false"}
    try:
        _req("POST", url, key, data=data, headers=hd); return True
    except urllib.error.HTTPError as e:
        # 400/409 quando já existe e upsert=false
        if e.code in (409,) and not upsert: return True
        raise

def download(base, key, bucket, path):
    """Baixa bytes de <bucket>/<path>. Retorna bytes ou None se não existir."""
    url = f"{base}/storage/v1/object/{bucket}/{urllib.parse.quote(path)}"
    try:
        return _req("GET", url, key).read()
    except urllib.error.HTTPError as e:
        if e.code in (400, 404): return None
        raise

def exists(base, key, bucket, path):
    url = f"{base}/storage/v1/object/info/{bucket}/{urllib.parse.quote(path)}"
    try:
        _req("GET", url, key); return True
    except urllib.error.HTTPError as e:
        if e.code in (400, 404): return False
        raise

def remove(base, key, bucket, paths):
    """Apaga uma lista de caminhos. paths = ['slot/nome', ...]. Retorna nº apagado."""
    if isinstance(paths, str): paths = [paths]
    url = f"{base}/storage/v1/object/{bucket}"
    body = json.dumps({"prefixes": paths}).encode()
    try:
        r = _req("DELETE", url, key, data=body, headers={"Content-Type": "application/json"})
        return len(json.loads(r.read().decode() or "[]"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 404): return 0
        raise

def listar(base, key, bucket, prefix="", limite=1000):
    """Lista objetos sob um prefixo (não recursivo). Retorna [{name,size,...}]."""
    url = f"{base}/storage/v1/object/list/{bucket}"
    body = json.dumps({"prefix": prefix, "limit": limite,
                       "sortBy": {"column": "name", "order": "asc"}}).encode()
    r = _req("POST", url, key, data=body, headers={"Content-Type": "application/json"})
    out = json.loads(r.read().decode() or "[]")
    res = []
    for o in out:
        meta = o.get("metadata") or {}
        res.append({"name": o.get("name"), "size": meta.get("size"), "id": o.get("id")})
    return res

def signed_url(base, key, bucket, path, expira_seg=3600):
    """Gera URL assinada temporária para leitura direta (usada pelo front)."""
    url = f"{base}/storage/v1/object/sign/{bucket}/{urllib.parse.quote(path)}"
    body = json.dumps({"expiresIn": int(expira_seg)}).encode()
    r = _req("POST", url, key, data=body, headers={"Content-Type": "application/json"})
    j = json.loads(r.read().decode())
    su = j.get("signedURL") or j.get("signedUrl") or ""
    return f"{base}/storage/v1{su}" if su.startswith("/") else su

def garantir_bucket(base, key, bucket, publico=False):
    """Cria o bucket se não existir. Idempotente."""
    url = f"{base}/storage/v1/bucket"
    body = json.dumps({"id": bucket, "name": bucket, "public": publico}).encode()
    try:
        _req("POST", url, key, data=body, headers={"Content-Type": "application/json"}); return True
    except urllib.error.HTTPError as e:
        if e.code in (400, 409): return True   # já existe
        raise
