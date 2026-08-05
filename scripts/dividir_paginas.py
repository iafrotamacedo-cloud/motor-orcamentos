#!/usr/bin/env python3
"""Utilitario da skill de orcamentos para lidar com notas multipagina e imagens.

Subcomandos:
  split    Divide um PDF de VARIAS notas em arquivos de 1 pagina, um por nota,
           com o nome que a rotina definir (ex.: "TICKET 125411 - NOTA 18058").
  fromimg  Converte uma imagem (.jpg/.jpeg/.png) numa nota PDF de 1 pagina.
  info     Diz quantas paginas tem o PDF e se ele tem camada de texto (para a
           rotina decidir se le por texto ou por imagem/OCR).

NADA e apagado aqui — quem move/roteia os arquivos e a rotina.

Exemplos:
  # quantas paginas + tem texto?
  python dividir_paginas.py info --pdf "CCF23072026_0007.pdf"

  # dividir: map.json = {"1":"TICKET 125411 - NOTA 18058","2":"TICKET 121541 - NOTA 18060"}
  python dividir_paginas.py split --pdf "CCF.pdf" --map map.json --outdir "/tmp/saida"

  # imagem solta -> PDF de 1 pagina
  python dividir_paginas.py fromimg --img "foto.jpg" --out "/tmp/TICKET 125999 - NOTA 123.pdf"
"""
import argparse, json, os, re, subprocess, sys

def _ensure(mod, pip_name=None):
    try:
        return __import__(mod)
    except ImportError:
        subprocess.run([sys.executable,"-m","pip","install",pip_name or mod,"--quiet","--break-system-packages"])
        return __import__(mod)

def sanitize(nome):
    nome=re.sub(r'[\\/:*?"<>|]+',' ',str(nome)).strip()
    nome=re.sub(r'\s+',' ',nome)
    return nome[:120] or "NOTA"

def cmd_info(a):
    npag=0
    try:
        out=subprocess.run(['pdfinfo',a.pdf],capture_output=True,text=True).stdout
        m=re.search(r'^Pages:\s*(\d+)',out,re.M); npag=int(m.group(1)) if m else 0
    except Exception: pass
    txt=subprocess.run(['pdftotext','-layout',a.pdf,'-'],capture_output=True,text=True).stdout
    # "tem texto" = pelo menos ~20 chars alfanumericos por pagina, em media
    alnum=len(re.sub(r'[^0-9A-Za-zÀ-ÿ]','',txt))
    tem_texto = npag>0 and (alnum/max(npag,1))>=20
    print(json.dumps({"paginas":npag,"tem_texto":tem_texto,"chars_alnum":alnum},ensure_ascii=False))

def cmd_split(a):
    pypdf=_ensure('pypdf')
    from pypdf import PdfReader, PdfWriter
    mapa=json.load(open(a.map,encoding='utf-8'))
    os.makedirs(a.outdir,exist_ok=True)
    reader=PdfReader(a.pdf)
    feitos=[]
    for i,page in enumerate(reader.pages, start=1):
        nome=mapa.get(str(i)) or mapa.get(i)
        if not nome:   # sem nome definido -> usa original + pagina
            base=os.path.splitext(os.path.basename(a.pdf))[0]
            nome=f"{base}_p{i}"
        dst=os.path.join(a.outdir, sanitize(nome)+".pdf")
        w=PdfWriter(); w.add_page(page)
        with open(dst,'wb') as f: w.write(f)
        feitos.append({"pagina":i,"arquivo":dst})
    print(json.dumps({"total":len(feitos),"arquivos":feitos},ensure_ascii=False,indent=1))

def cmd_fromimg(a):
    Image=_ensure('PIL.Image','pillow') and __import__('PIL.Image',fromlist=['Image'])
    from PIL import Image
    im=Image.open(a.img)
    if im.mode in ('RGBA','P','LA'): im=im.convert('RGB')
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or '.',exist_ok=True)
    im.save(a.out,'PDF',resolution=200.0)
    print(json.dumps({"arquivo":a.out},ensure_ascii=False))

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('info'); p.add_argument('--pdf',required=True); p.set_defaults(fn=cmd_info)
    p=sub.add_parser('split'); p.add_argument('--pdf',required=True); p.add_argument('--map',required=True); p.add_argument('--outdir',required=True); p.set_defaults(fn=cmd_split)
    p=sub.add_parser('fromimg'); p.add_argument('--img',required=True); p.add_argument('--out',required=True); p.set_defaults(fn=cmd_fromimg)
    a=ap.parse_args(); a.fn(a)

if __name__=='__main__': main()
