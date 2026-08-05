#!/usr/bin/env python3
"""
Prepara os itens do orçamento a partir dos itens BRUTOS da nota:
- soma 20% ao valor unitário de cada item (SEM mencionar);
- se houver item de ENTREGA/FRETE (ex.: "SERVIÇO DE ENTREGA", "TAXA DE ENTREGA"),
  soma os 20% nele também, mas o REMOVE do orçamento e RATEIA seu custo
  (proporcional ao total de cada item) entre os demais itens;
- devolve os itens formatados (R$, vírgula), o total geral e o valor por extenso.

Entrada (stdin ou --input JSON):
  {"itens":[{"desc":"...","quant":10,"unid":"UN","unit":1.30},
            {"desc":"SERVIÇO DE ENTREGA","quant":1,"unid":"SV","unit":50.0}]}
  (unit = valor unitário BRUTO da nota, sem markup; "entrega":true força marcar)

Saída (stdout JSON):
  {"itens":[{"item":1,"desc":..,"quant":"10,00","unid":"UN","valor_unit":"R$ 1,56","total":"R$ 15,60"}...],
   "total_geral":"R$ 32,58","total_geral_num":32.58,"extenso":"...",
   "entrega":{"havia":true,"total_rateado":"R$ 60,00"}}
"""
import argparse,json,os,re,sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
try:
    from extenso import moeda_ext
except Exception:
    def moeda_ext(v): return ""

MARKUP=1.20
RE_ENTREGA=re.compile(r"servi[çc]o\s+de\s+entrega|taxa\s+de\s+entrega|\bentrega\b|\bfrete\b",re.I)

def money(v):
    return "R$ "+(f"{v:,.2f}".replace(",","X").replace(".",",").replace("X","."))
def qfmt(q):
    return f"{q:,.2f}".replace(",","X").replace(".",",").replace("X",".")
def is_entrega(it):
    if it.get("entrega") is True: return True
    if it.get("entrega") is False: return False
    return bool(RE_ENTREGA.search(str(it.get("desc",""))))

def preparar(itens):
    norm=[]; entrega=[]
    for it in itens:
        q=float(it.get("quant",0) or 0); u=float(it.get("unit",0) or 0)
        vu=round(u*MARKUP,2); tot=round(q*vu,2)
        rec={"desc":str(it.get("desc","")).strip(),"quant":q,"unid":str(it.get("unid","") or "UN"),
             "valor_unit":vu,"total":tot}
        (entrega if is_entrega(it) else norm).append(rec)
    entrega_total=round(sum(e["total"] for e in entrega),2)
    base=round(sum(n["total"] for n in norm),2)
    if entrega_total>0 and norm and base>0:
        for n in norm:
            alvo=n["total"]+entrega_total*(n["total"]/base)
            n["valor_unit"]=round(alvo/n["quant"],2) if n["quant"] else round(alvo,2)
            n["total"]=round(n["quant"]*n["valor_unit"],2)
    saida=[]
    for i,n in enumerate(norm,1):
        saida.append({"item":i,"desc":n["desc"],"quant":qfmt(n["quant"]),"unid":n["unid"],
                      "valor_unit":money(n["valor_unit"]),"total":money(n["total"])})
    tg=round(sum(n["total"] for n in norm),2)
    return {"itens":saida,"total_geral":money(tg),"total_geral_num":tg,
            "extenso":moeda_ext(tg)+".",
            "entrega":{"havia":bool(entrega),"total_rateado":money(entrega_total) if entrega else "R$ 0,00"}}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",default=None)
    args=ap.parse_args()
    data=json.load(open(args.input,encoding="utf-8")) if args.input else json.load(sys.stdin)
    itens=data.get("itens",data) if isinstance(data,dict) else data
    print(json.dumps(preparar(itens),ensure_ascii=False,indent=1))

if __name__=="__main__":
    main()
