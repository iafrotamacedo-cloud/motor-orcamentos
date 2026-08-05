#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera o PDF do orçamento a partir do dados.json (reportlab — Python puro, sem LibreOffice).
Uso: python gerar_pdf.py <dados.json> <saida.pdf> <logo.jpg>
"""
import sys, json, os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Table, TableStyle,
                                Paragraph, Spacer, Image, HRFlowable)

NAVY=HexColor("#1F3864"); ZEBRA=HexColor("#F3F5F8"); GRAYBAR=HexColor("#E9ECF1")
TOTALBAR=HexColor("#DCE1EA"); LINE=HexColor("#C9CFDA"); GRAY=HexColor("#5A6472"); WHITE=HexColor("#FFFFFF")

def P(txt,size=8.5,bold=False,ital=False,color=None,align=TA_LEFT,leading=None):
    st=ParagraphStyle("s",fontName=("Helvetica-BoldOblique" if bold and ital else
                                    "Helvetica-Bold" if bold else
                                    "Helvetica-Oblique" if ital else "Helvetica"),
                      fontSize=size,leading=leading or size*1.25,textColor=color or HexColor("#1E1E1E"),
                      alignment=align)
    return Paragraph(str(txt if txt is not None else ""),st)

def kv(lab,val):
    return Paragraph(f'<b>{lab}</b>{"" if val in (None,"") else val if val else "—"}',
                     ParagraphStyle("kv",fontName="Helvetica",fontSize=8.5,leading=11.5))

def card(titulo,linhas,w):
    body=[[Paragraph(titulo,ParagraphStyle("ct",fontName="Helvetica-Bold",fontSize=8.5,textColor=WHITE))]]
    for lab,val in linhas: body.append([kv(lab,val)])
    t=Table(body,colWidths=[w])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(0,0),NAVY),
        ("BOX",(0,0),(-1,-1),0.6,LINE),("LINEBELOW",(0,0),(0,0),0.6,NAVY),
        ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
        ("TOPPADDING",(0,0),(0,0),3),("BOTTOMPADDING",(0,0),(0,0),3),
        ("TOPPADDING",(0,1),(-1,-1),2.5),("BOTTOMPADDING",(0,1),(-1,-1),2.5),
    ]))
    return t

def build(dj,out,logo):
    F=dj["faturamento"]; C=dj["cliente"]
    PW=190*mm
    doc=BaseDocTemplate(out,pagesize=A4,leftMargin=10*mm,rightMargin=10*mm,topMargin=10*mm,bottomMargin=14*mm)
    def footer(canvas,d):
        canvas.saveState(); canvas.setFont("Helvetica",7); canvas.setFillColor(HexColor("#8A93A0"))
        canvas.drawCentredString(A4[0]/2,9*mm,"Frota Macedo Engenharia LTDA  •  CNPJ 27.363.223/0001-70")
        canvas.restoreState()
    frame=Frame(doc.leftMargin,doc.bottomMargin,PW,A4[1]-doc.topMargin-doc.bottomMargin,
                leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p",frames=[frame],onPage=footer)])
    S=[]

    # cabeçalho
    empresa=[P("FROTA MACEDO ENGENHARIA LTDA",13,bold=True,color=NAVY,leading=15),
             P("Eng. Heitor de Oliveira Albuquerque, 295 — Cidade dos Funcionários — Fortaleza/CE",7.5,color=GRAY,leading=10),
             P("(85) 2181-1386  •  frotamacedoengenharia@gmail.com  •  CNPJ 27.363.223/0001-70",7.5,color=GRAY,leading=10)]
    dirbox=[P(dj.get("data",""),8,color=GRAY,align=TA_RIGHT),
            P("Orçamento nº "+str(dj["numero_ticket"]),8,color=GRAY,align=TA_RIGHT)]
    logo_cell=""
    if logo and os.path.exists(logo):
        try: logo_cell=Image(logo,width=32*mm,height=16*mm)
        except Exception: logo_cell=""
    cab=Table([[logo_cell,empresa,dirbox]],colWidths=[34*mm,110*mm,46*mm])
    cab.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),0),
                             ("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)]))
    S+=[cab,Spacer(1,3),HRFlowable(width=PW,thickness=1.4,color=NAVY),Spacer(1,4)]

    # barra título
    tit=Table([[P("ORÇAMENTO Nº "+str(dj["numero_ticket"]),15,bold=True,color=WHITE)],
               [P("REVISÃO "+str(dj.get("revisao",1)),8,color=HexColor("#D6DCE6"))]],colWidths=[PW])
    tit.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),NAVY),("LEFTPADDING",(0,0),(-1,-1),6),
                             ("TOPPADDING",(0,0),(0,0),4),("BOTTOMPADDING",(0,0),(0,0),0),
                             ("TOPPADDING",(0,1),(0,1),0),("BOTTOMPADDING",(0,1),(0,1),4)]))
    S+=[tit,Spacer(1,3)]

    # obra
    obra=Table([[Paragraph("<b>OBRA:  </b>"+str(dj.get("obra","")),
                 ParagraphStyle("o",fontName="Helvetica",fontSize=9,textColor=NAVY,leading=11))]],colWidths=[PW])
    obra.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),GRAYBAR),("LEFTPADDING",(0,0),(-1,-1),6),
                              ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    S+=[obra,Spacer(1,5)]

    # cartões
    cw=(PW-8*mm)/2
    fat=card("DADOS DO PRESTADOR",[("Nome: ",F.get("nome")),("CNPJ: ",F.get("cnpj")),
             ("Forma de pagamento: ",F.get("forma_pagamento")),("Data de faturamento: ",F.get("data_faturamento"))],cw)
    cli=card("DADOS DO TOMADOR",[("Nome: ",C.get("nome")),("CNPJ: ",C.get("cnpj")),
             ("Endereço: ",C.get("endereco")),("Cidade/Estado: ",C.get("cidade"))],cw)
    dois=Table([[fat,"",cli]],colWidths=[cw,8*mm,cw])
    dois.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),
                              ("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)]))
    S+=[dois,Spacer(1,6),P("DISCRIMINAÇÃO DOS ITENS",9.5,bold=True,color=NAVY),Spacer(1,2)]

    # tabela de itens
    cols=[13.71,80.31,21.55,17.63,28.4,28.4]; cw2=[c/sum(cols)*PW for c in cols]
    hs=ParagraphStyle("h",fontName="Helvetica-Bold",fontSize=8,textColor=WHITE)
    def cell(t,align=TA_LEFT,bold=False,color=None):
        return Paragraph(str(t),ParagraphStyle("c",fontName="Helvetica-Bold" if bold else "Helvetica",
                        fontSize=8,leading=10,alignment=align,textColor=color or HexColor("#1E1E1E")))
    data=[[Paragraph(h,hs) for h in ["ITEM","DESCRIÇÃO","QUANT.","UNID.","VALOR UNIT.","TOTAL"]]]
    for it in dj["itens"]:
        data.append([cell(it["item"],TA_CENTER),cell(it["desc"]),cell(it["quant"],TA_CENTER),
                     cell(it["unid"],TA_CENTER),cell(it["valor_unit"],TA_RIGHT),cell(it["total"],TA_RIGHT)])
    data.append([cell("TOTAL GERAL",TA_RIGHT,bold=True),"","","","",cell(dj["total_geral"],TA_RIGHT,bold=True)])
    tb=Table(data,colWidths=cw2,repeatRows=1)
    ts=[("BACKGROUND",(0,0),(-1,0),NAVY),("GRID",(0,0),(-1,-1),0.4,LINE),
        ("ALIGN",(0,0),(0,0),"CENTER"),("ALIGN",(2,0),(3,0),"CENTER"),("ALIGN",(4,0),(5,0),"RIGHT"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ("SPAN",(0,-1),(4,-1)),("BACKGROUND",(0,-1),(-1,-1),TOTALBAR)]
    for i in range(1,len(dj["itens"])+1):
        if i%2==0: ts.append(("BACKGROUND",(0,i),(-1,i),ZEBRA))
    tb.setStyle(TableStyle(ts))
    S+=[tb,Spacer(1,3),
        Paragraph(f'<b>Valor total por extenso:  </b><i>{dj.get("extenso","")}</i>',
                  ParagraphStyle("ex",fontName="Helvetica",fontSize=8.5,leading=11)),Spacer(1,5)]

    # observações
    obs=dj.get("observacoes") or ["Orçamento válido por 7 (sete) dias corridos a partir da data de emissão.",
                                  "Os valores acima incluem material e serviço de entrega."]
    ob=[[Paragraph("OBSERVAÇÕES",ParagraphStyle("obt",fontName="Helvetica-Bold",fontSize=8.5,textColor=WHITE))]]
    for o in obs: ob.append([Paragraph("•  "+str(o),ParagraphStyle("ol",fontName="Helvetica",fontSize=8.5,leading=11))])
    obt=Table(ob,colWidths=[PW])
    obt.setStyle(TableStyle([("BACKGROUND",(0,0),(0,0),NAVY),("BOX",(0,0),(-1,-1),0.6,LINE),
        ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
        ("TOPPADDING",(0,0),(0,0),3),("BOTTOMPADDING",(0,0),(0,0),3),
        ("TOPPADDING",(0,1),(-1,-1),2.5),("BOTTOMPADDING",(0,1),(-1,-1),2.5)]))
    S+=[obt,Spacer(1,16)]

    # assinaturas
    sig=Table([[P("Frota Macedo Engenharia LTDA",8,color=GRAY,align=TA_CENTER),"",
                P("Aceite do Cliente — Nome / Data",8,color=GRAY,align=TA_CENTER)]],colWidths=[cw,8*mm,cw])
    sig.setStyle(TableStyle([("LINEABOVE",(0,0),(0,0),0.5,HexColor("#8A93A0")),
                             ("LINEABOVE",(2,0),(2,0),0.5,HexColor("#8A93A0")),
                             ("TOPPADDING",(0,0),(-1,-1),3)]))
    S+=[sig]
    doc.build(S)

if __name__=="__main__":
    build(json.load(open(sys.argv[1],encoding="utf-8")),sys.argv[2],sys.argv[3] if len(sys.argv)>3 else None)
    print("OK",sys.argv[2])
