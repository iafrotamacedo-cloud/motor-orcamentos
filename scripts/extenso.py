#!/usr/bin/env python3
"""Valor (reais) por extenso. Uso: python extenso.py 306.84"""
import sys
U=["zero","um","dois","três","quatro","cinco","seis","sete","oito","nove","dez",
"onze","doze","treze","quatorze","quinze","dezesseis","dezessete","dezoito","dezenove"]
DEZ=["","","vinte","trinta","quarenta","cinquenta","sessenta","setenta","oitenta","noventa"]
CEM=["","cento","duzentos","trezentos","quatrocentos","quinhentos","seiscentos","setecentos","oitocentos","novecentos"]
def tres(n):
    if n==0: return ""
    if n==100: return "cem"
    p=[]
    c=n//100; d=(n%100)//10; u=n%10; r=n%100
    if c: p.append(CEM[c])
    if r<20 and r>0: p.append(U[r])
    else:
        if d: p.append(DEZ[d])
        if u: p.append(U[u])
    return " e ".join(p)
def ext_int(n):
    if n==0: return "zero"
    grupos=[]; i=0
    for div,sing,plur in [(1,"",""),(1000,"mil","mil"),(1000000,"milhão","milhões"),(1000000000,"bilhão","bilhões")]:
        pass
    partes=[]
    escalas=[("",""),("mil","mil"),("milhão","milhões"),("bilhão","bilhões")]
    blocos=[]
    while n>0: blocos.append(n%1000); n//=1000
    nome=[]
    for idx in range(len(blocos)-1,-1,-1):
        b=blocos[idx]
        if b==0: continue
        txt=tres(b)
        if idx==1: txt=("mil" if b==1 else txt+" mil")
        elif idx>=2:
            sing,plur=escalas[idx]
            txt=txt+" "+(sing if b==1 else plur)
        nome.append(txt)
    # juntar com "e" conforme regra simples
    return ", ".join(nome).replace(", mil","mil") if len(nome)>1 else nome[0]
def moeda_ext(v):
    reais=int(v); cent=int(round((v-reais)*100))
    if cent==60: pass
    r=ext_int(reais)+(" real" if reais==1 else " reais")
    if cent>0:
        c=tres(cent)+(" centavo" if cent==1 else " centavos")
        return r+" e "+c
    return r
if __name__=="__main__":
    v=float(sys.argv[1].replace(",","."))
    print(moeda_ext(v))
