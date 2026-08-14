# -*- coding: utf-8 -*-
"""
FrotaHub — Camada de arquivos hot/cold + Backup (Supabase Storage <-> Dropbox).

Wiring no app.py (3 linhas):
    import supabase_storage as sst
    import backup_endpoints
    backup_endpoints.montar(app, dict(
        exige=exige, verifica_pin=_verifica_pin, dropbox=dropbox_rateio,
        sb_url=SB_URL, sb_key=SB_KEY, bucket=os.environ.get("SUPABASE_BUCKET","frotahub"),
        agora=_agora, hoje=_hoje, cfg_get=_cfg_get, cfg_set=_cfg_set, log=log_frotahub, sst=sst,
    ))
E adicione "/backup" à lista de prefixos isentos do _basic_auth (ASGI).

REGRAS DE OURO:
  • Backup de ROTINA/AUTO só apaga do Supabase DEPOIS de confirmar a cópia no Dropbox.
  • Backup de SEGURANÇA nunca apaga nada (só espelha) e não muda acesso.
  • Nada é apagado do Dropbox em nenhuma rotina.
"""
import os, json, hashlib, datetime, urllib.request, urllib.parse, urllib.error

SUPA_LIMITE   = 1024 * 1024 * 1024        # 1 GB  (Storage free)
EGRESS_LIMITE = 5 * 1024 * 1024 * 1024    # 5 GB/mês (interação free)
DROPBOX_BASE  = os.environ.get("DROPBOX_BASE", "/FROTAHUB").rstrip("/")
ARQ_RAIZ      = DROPBOX_BASE + "/_ARQUIVOS"    # cofre frio (espelho permanente)

# Slots = "pastas" que aparecem na cascata do Enviar para FrotaHub
SLOTS = [
    {"slot": "notas_orc",        "rotulo": "Notas para orçamento"},
    {"slot": "orc_nao_lancados", "rotulo": "Orçamentos não lançados"},
    {"slot": "pco_oc",           "rotulo": "Ordens de compra (PCO)"},
    {"slot": "rateio",           "rotulo": "Rateio"},
    {"slot": "diversos",         "rotulo": "Diversos"},
]
SLOT_IDS = {s["slot"] for s in SLOTS}

def montar(app, D):
    from fastapi import Request, HTTPException
    from fastapi.responses import Response, RedirectResponse

    sst   = D["sst"]
    dbx   = D["dropbox"]
    SBU   = D["sb_url"]; SBK = D["sb_key"]; BUCKET = D["bucket"]
    agora = D["agora"]; hoje = D["hoje"]
    cfg_get = D["cfg_get"]; cfg_set = D["cfg_set"]
    exige = D["exige"]; verifica_pin = D["verifica_pin"]; logf = D.get("log", lambda *a, **k: None)

    # ---------------- helpers de banco (PostgREST) ----------------
    def _db(method, path, data=None, prefer=None):
        h = {"apikey": SBK, "authorization": f"Bearer {SBK}", "content-type": "application/json"}
        if prefer: h["prefer"] = prefer
        rq = urllib.request.Request(f"{SBU}/rest/v1/{path}",
                                    data=(json.dumps(data).encode() if data is not None else None),
                                    method=method, headers=h)
        r = urllib.request.urlopen(rq, timeout=30)
        raw = r.read().decode()
        return json.loads(raw) if raw else []

    def _reg_get(filtro=""):
        return _db("GET", f"arq_registro?{filtro}")

    def _reg_upsert(row):
        _db("POST", "arq_registro?on_conflict=slot,nome", data=row,
            prefer="resolution=merge-duplicates,return=minimal")

    def _reg_patch(rid, patch):
        _db("PATCH", f"arq_registro?id=eq.{rid}", data=patch, prefer="return=minimal")

    def _egress_add(nbytes):
        mes = agora().strftime("%Y-%m")
        try:
            cur = _db("GET", f"egress_mensal?mes=eq.{mes}&select=bytes")
            atual = (cur[0]["bytes"] if cur else 0) + int(nbytes or 0)
            _db("POST", "egress_mensal?on_conflict=mes",
                data={"mes": mes, "bytes": atual}, prefer="resolution=merge-duplicates,return=minimal")
        except Exception:
            pass

    def _egress_mes():
        mes = agora().strftime("%Y-%m")
        try:
            cur = _db("GET", f"egress_mensal?mes=eq.{mes}&select=bytes")
            return cur[0]["bytes"] if cur else 0
        except Exception:
            return 0

    def _supa_usado():
        """Soma o tamanho dos arquivos que estão QUENTES (no Supabase)."""
        try:
            linhas = _db("GET", "arq_registro?storage=eq.supabase&select=tamanho&limit=100000")
            return sum(int(x.get("tamanho") or 0) for x in linhas)
        except Exception:
            return 0

    # ---------------- helpers de Dropbox (cofre frio) ----------------
    def _dbx_token():
        return dbx.obter_token()

    def _dbx_api(endpoint, arg, access):
        rq = urllib.request.Request("https://api.dropboxapi.com/2/" + endpoint,
            data=json.dumps(arg).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(rq, timeout=30).read().decode())

    def _dbx_espaco():
        try:
            access = _dbx_token()
            rq = urllib.request.Request("https://api.dropboxapi.com/2/users/get_space_usage",
                data=b"null", method="POST",
                headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
            j = json.loads(urllib.request.urlopen(rq, timeout=30).read().decode())
            usado = j.get("used", 0)
            alloc = (j.get("allocation") or {}).get("allocated", 0)
            return usado, alloc
        except Exception:
            return 0, 0

    def _dbx_existe(path, access):
        try:
            _dbx_api("files/get_metadata", {"path": path}, access); return True
        except urllib.error.HTTPError:
            return False
        except Exception:
            return False

    def _cold_path(slot, nome):
        return f"{ARQ_RAIZ}/{slot}/{nome}"

    def _espelhar(slot, nome, dados, access):
        """Garante a cópia no Dropbox (cofre frio). Retorna True se está lá ao fim."""
        path = _cold_path(slot, nome)
        if _dbx_existe(path, access):
            return True
        try:
            dbx.subir_bytes(access, dados, path, overwrite=True)
            return _dbx_existe(path, access)
        except Exception:
            return False

    # ---------------- autorização ----------------
    def _builder(request):
        u, p = exige(request, "GERAR_ORCAMENTOS")
        if p.get("nivel") != "builder":
            raise HTTPException(403, "apenas builder")
        return u, p

    def _pin_ok(request, u, p, corpo):
        pin = str((corpo or {}).get("pin", "") or "")
        if not verifica_pin(u["id"], p.get("nivel"), pin):
            raise HTTPException(403, "PIN inválido")

    # =====================================================================
    #  PAINEL — medidores + configuração
    # =====================================================================
    @app.get("/backup/painel")
    def backup_painel(request: Request):
        _builder(request)
        supa = _supa_usado(); egr = _egress_mes()
        dbx_usado, dbx_alloc = _dbx_espaco()
        cfg = cfg_get("backup_cfg", {}) or {}
        # resumo do registro (quente x frio, e por dia)
        try:
            q = _db("GET", "arq_registro?select=storage&limit=100000")
            quentes = sum(1 for x in q if x.get("storage") == "supabase")
            frios   = sum(1 for x in q if x.get("storage") == "dropbox")
        except Exception:
            quentes = frios = 0
        return {
            "supabase":  {"usado": supa, "limite": SUPA_LIMITE,
                          "pct": round(100 * supa / SUPA_LIMITE, 1)},
            "interacao": {"usado": egr, "limite": EGRESS_LIMITE,
                          "pct": round(100 * egr / EGRESS_LIMITE, 1), "mes": agora().strftime("%Y-%m")},
            "dropbox":   {"usado": dbx_usado, "limite": dbx_alloc,
                          "pct": (round(100 * dbx_usado / dbx_alloc, 1) if dbx_alloc else None)},
            "arquivos":  {"quentes": quentes, "frios": frios},
            "cfg": cfg, "slots": SLOTS, "agora": agora().isoformat(),
        }

    @app.post("/backup/config")
    async def backup_config(request: Request):
        b = await request.json()
        u, p = _builder(request); _pin_ok(request, u, p, b)
        cfg = cfg_get("backup_cfg", {}) or {}
        if "dias_retencao" in b:  cfg["dias_retencao"] = max(1, int(b["dias_retencao"]))
        if "auto_teto_pct" in b:  cfg["auto_teto_pct"] = min(99, max(50, int(b["auto_teto_pct"])))
        if "agenda" in b and isinstance(b["agenda"], dict):
            cfg["agenda"] = {**cfg.get("agenda", {}), **b["agenda"]}
        cfg_set("backup_cfg", cfg)
        logf(u["id"], p.get("papel"), "GERAR_ORCAMENTOS", "BACKUP_CONFIG", json.dumps(cfg)[:180])
        return {"ok": True, "cfg": cfg}

    @app.post("/backup/device_token")
    async def backup_device_token(request: Request):
        """Mostra (ou gera) o token do dispositivo usado pelo 'Enviar para FrotaHub' do desktop."""
        b = await request.json()
        u, p = _builder(request); _pin_ok(request, u, p, b)
        tk = str(cfg_get("device_token", "") or "")
        if b.get("gerar") or not tk:
            import secrets as _s
            tk = _s.token_urlsafe(24); cfg_set("device_token", tk)
            logf(u["id"], p.get("papel"), "GERAR_ORCAMENTOS", "DEVICE_TOKEN", "gerado")
        return {"token": tk}

    # =====================================================================
    #  RODAR BACKUP (manual) — segurança ou rotina
    # =====================================================================
    def _rodar(tipo, disparo):
        """tipo: 'seguranca' | 'rotina'. Retorna dict com contadores."""
        access = _dbx_token()
        copiados = 0; removidos = 0; detalhe = []
        if tipo == "seguranca":
            # espelha TODOS os quentes que ainda não estão no Dropbox (não apaga nada)
            quentes = _reg_get("storage=eq.supabase&select=id,slot,nome,tamanho&limit=100000")
            for r in quentes:
                dados = sst.download(SBU, SBK, BUCKET, f"{r['slot']}/{r['nome']}")
                if dados is None:
                    continue
                if _espelhar(r["slot"], r["nome"], dados, access):
                    copiados += 1
            return {"tipo": tipo, "copiados": copiados, "removidos": 0}

        # ---- ROTINA: janela deslizante ----
        cfg = cfg_get("backup_cfg", {}) or {}
        dias = max(1, int(cfg.get("dias_retencao", 32)))
        corte = hoje() - datetime.timedelta(days=dias)   # dia <= corte  => despeja
        velhos = _reg_get(f"storage=eq.supabase&dia=lte.{corte.isoformat()}&select=id,slot,nome,tamanho&limit=100000")
        for r in velhos:
            dados = sst.download(SBU, SBK, BUCKET, f"{r['slot']}/{r['nome']}")
            if dados is not None:
                if not _espelhar(r["slot"], r["nome"], dados, access):
                    detalhe.append(f"pulei {r['slot']}/{r['nome']} (sem confirmar cópia no Dropbox)")
                    continue   # REGRA DE OURO: não apaga sem cópia confirmada
            elif not _dbx_existe(_cold_path(r["slot"], r["nome"]), access):
                detalhe.append(f"pulei {r['slot']}/{r['nome']} (não achei no Supabase nem no Dropbox)")
                continue
            # cópia garantida -> apaga do Supabase e vira o acesso p/ Dropbox
            sst.remove(SBU, SBK, BUCKET, f"{r['slot']}/{r['nome']}")
            _reg_patch(r["id"], {"storage": "dropbox", "movido_em": agora().isoformat()})
            removidos += 1
        return {"tipo": tipo, "copiados": copiados, "removidos": removidos, "corte": corte.isoformat(), "detalhe": detalhe}

    @app.post("/backup/rodar")
    async def backup_rodar(request: Request):
        b = await request.json()
        u, p = _builder(request); _pin_ok(request, u, p, b)
        tipo = (b.get("tipo") or "seguranca").strip()
        if tipo not in ("seguranca", "rotina"):
            raise HTTPException(400, "tipo inválido")
        res = _rodar(tipo, "manual")
        try:
            _db("POST", "backup_log", data={"tipo": tipo, "disparo": "manual",
                "copiados": res.get("copiados", 0), "removidos": res.get("removidos", 0),
                "detalhe": {"corte": res.get("corte"), "notas": res.get("detalhe")}},
                prefer="return=minimal")
        except Exception:
            pass
        logf(u["id"], p.get("papel"), "GERAR_ORCAMENTOS", "BACKUP_RODAR", tipo)
        return {"ok": True, **res}

    @app.post("/backup/auto_check")
    async def backup_auto_check(request: Request):
        """Chamado por cron/robot: se o Supabase passou do teto, roda ROTINA até baixar."""
        # aceita robot key OU builder
        rk = request.headers.get("x-robot-key", "")
        if rk and rk == os.environ.get("ROBOT_KEY", "___"):
            pass
        else:
            _builder(request)
        cfg = cfg_get("backup_cfg", {}) or {}
        teto = int(cfg.get("auto_teto_pct", 85))
        pct = 100 * _supa_usado() / SUPA_LIMITE
        if pct < teto:
            return {"ok": True, "rodou": False, "pct": round(pct, 1), "teto": teto}
        res = _rodar("rotina", "limite")
        try:
            _db("POST", "backup_log", data={"tipo": "auto", "disparo": "limite",
                "copiados": res.get("copiados", 0), "removidos": res.get("removidos", 0),
                "detalhe": {"pct_antes": round(pct, 1), "teto": teto}}, prefer="return=minimal")
        except Exception:
            pass
        return {"ok": True, "rodou": True, "pct_antes": round(pct, 1), **res}

    @app.post("/backup/agenda_tick")
    async def backup_agenda_tick(request: Request):
        """Chamado de hora em hora por um cron. Roda o backup agendado quando bate o
        dia/hora escolhido pelo builder (uma vez por dia). Sempre checa o teto também."""
        rk = request.headers.get("x-robot-key", "")
        if not (rk and rk == os.environ.get("ROBOT_KEY", "___")):
            _builder(request)
        cfg = cfg_get("backup_cfg", {}) or {}
        ag = cfg.get("agenda", {}) or {}
        now = agora(); hoje_iso = now.date().isoformat()
        acoes = []
        # 1) proteção por teto (sempre)
        teto = int(cfg.get("auto_teto_pct", 85))
        if 100 * _supa_usado() / SUPA_LIMITE >= teto:
            r = _rodar("rotina", "limite"); acoes.append({"limite": r})
        # 2) agenda do builder
        if ag.get("ativo"):
            try: hora_ok = now.hour == int(str(ag.get("hora", "02:00"))[:2])
            except Exception: hora_ok = False
            dia_ok = (ag.get("freq") == "diario") or (int(ag.get("dia_semana", 0)) == now.weekday())
            ja_hoje = ag.get("ultimo_run") == hoje_iso
            if hora_ok and dia_ok and not ja_hoje:
                tipo = ag.get("tipo", "rotina")
                r = _rodar(tipo, "agenda"); acoes.append({"agenda": r})
                ag["ultimo_run"] = hoje_iso; cfg["agenda"] = ag; cfg_set("backup_cfg", cfg)
                try:
                    _db("POST", "backup_log", data={"tipo": tipo, "disparo": "agenda",
                        "copiados": r.get("copiados", 0), "removidos": r.get("removidos", 0),
                        "detalhe": {"corte": r.get("corte")}}, prefer="return=minimal")
                except Exception: pass
        return {"ok": True, "acoes": acoes, "agora": now.isoformat()}

    # =====================================================================
    #  ARQUIVOS — enviar (SendTo/PWA), listar slots, ver (hot/cold)
    # =====================================================================
    @app.get("/backup/slots")
    def backup_slots(request: Request):
        _auth_upar(request)   # aceita login normal OU device_token (desktop)
        return {"slots": SLOTS}

    def _auth_upar(request):
        """Aceita device_token (desktop) OU login normal."""
        dt = request.headers.get("x-device-token", "")
        if dt and dt == str(cfg_get("device_token", "") or ""):
            return ("device", {"id": "device", "papel": "device", "nivel": "device"})
        return exige(request, "GERAR_ORCAMENTOS")

    @app.post("/backup/upar")
    async def backup_upar(request: Request):
        u, p = _auth_upar(request)
        form = await request.form()
        slot = (form.get("slot") or "diversos").strip()
        if slot not in SLOT_IDS:
            raise HTTPException(400, "slot inválido")
        files = form.getlist("arquivo") or form.getlist("arquivos")
        if not files:
            raise HTTPException(400, "nenhum arquivo")
        salvos = []; erros = []
        for f in files:
            nome = os.path.basename(getattr(f, "filename", "") or "")
            if not nome:
                continue
            try:
                dados = await f.read()
                if len(dados) > 30 * 1024 * 1024:
                    erros.append(f"{nome}: acima de 30 MB"); continue
                h = hashlib.sha256(dados).hexdigest()
                sst.upload(SBU, SBK, BUCKET, f"{slot}/{nome}", dados, upsert=True)
                _reg_upsert({"slot": slot, "nome": nome, "caminho": f"{slot}/{nome}",
                             "dia": hoje().isoformat(), "tamanho": len(dados),
                             "storage": "supabase", "hash": h})
                salvos.append(nome)
            except Exception as e:
                erros.append(f"{nome}: {str(e)[:80]}")
        logf(u.get("id"), p.get("papel"), "GERAR_ORCAMENTOS", "ARQ_UPAR", f"{len(salvos)} em {slot}")
        return {"salvos": salvos, "erros": erros, "slot": slot}

    @app.get("/backup/arq")
    def backup_arq(request: Request, slot: str = "", nome: str = ""):
        exige(request, "GERAR_ORCAMENTOS")
        if slot not in SLOT_IDS or "/" in nome or ".." in nome:
            raise HTTPException(400, "parâmetros inválidos")
        reg = _reg_get(f"slot=eq.{urllib.parse.quote(slot)}&nome=eq.{urllib.parse.quote(nome)}&select=storage,tamanho&limit=1")
        if not reg:
            raise HTTPException(404, "arquivo não registrado")
        if reg[0]["storage"] == "supabase":
            _egress_add(reg[0].get("tamanho") or 0)   # conta interação (aprox.)
            url = sst.signed_url(SBU, SBK, BUCKET, f"{slot}/{nome}", 3600)
            return RedirectResponse(url)
        # frio: entrega do Dropbox
        try:
            dados = dbx.baixar(_dbx_token(), _cold_path(slot, nome))
        except Exception:
            dados = None
        if dados is None:
            raise HTTPException(404, "arquivo não encontrado no cofre")
        ct = sst._ctype(nome)
        return Response(content=dados, media_type=ct,
                        headers={"Content-Disposition": f'inline; filename="{urllib.parse.quote(nome)}"'})
