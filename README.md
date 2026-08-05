# Motor de Orçamentos — Frota Macedo

App online: o operador sobe as **notas/DAVs** (PDF ou imagem) e, opcionalmente, a
**planilha de controle** do mês, clica em **Gerar** e baixa um **ZIP** com os
**orçamentos em PDF** (por Mês/Loja), a planilha de controle atualizada e a
**planilha de correção** das notas que caíram no filtro (sem ticket / ticket não associado).
Se os segredos do Dropbox estiverem configurados, ele também **rateia as notas direto no Dropbox online**.

- **Leitura das notas:** Google Gemini (visão).
- **Ticket → loja:** tabela `chamados` no Supabase (alimentada pelo robô do Trílogo).
- **Regras:** +20% no unitário, rateio de entrega, 1 orçamento por ticket.
- **PDF:** gerado em Python (reportlab) — sem Node, sem LibreOffice → imagem leve, cabe em host grátis sem cartão.

## Publicar no Render.com (grátis, sem cartão)
1. Suba estes arquivos para um repositório no GitHub (app.py, Dockerfile, requirements.txt, `scripts/`, `assets/`, dropbox_rateio.py).
2. Em render.com → **New → Web Service** → conecte o repositório.
3. **Runtime: Docker** (ele acha o Dockerfile). **Instance type: Free**.
4. Em **Environment**, adicione as variáveis:
   - `GEMINI_API_KEY`   (https://aistudio.google.com/apikey)
   - `SUPABASE_URL`      (https://faalgfbugvekbuhhtatt.supabase.co)
   - `SUPABASE_SERVICE_KEY`  (Supabase → Project Settings → API → service_role)
   - `APP_USER` / `APP_PASS`  (login do motor)
   - (opcionais) `GEMINI_MODEL`, `FAT_NOME`, `FAT_CNPJ`
   - (Dropbox) `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`, `DROPBOX_BASE`
5. **Create Web Service**. O 1º build leva alguns minutos; depois abre numa URL `...onrender.com`.

Obs.: no plano Free o serviço "dorme" após ~15 min sem uso — o primeiro acesso do dia demora ~30-60s pra acordar.
