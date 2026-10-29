"""Explicit per-job SMTP send. Credentials only from the local environment."""
import hashlib
import json
import os
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from fastapi import HTTPException
from automation import event


def recipient(job):
    matches=re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",job["description"] or "")
    matches=list(dict.fromkeys(m.rstrip('.') for m in matches))
    matches=[m for m in matches if not re.search(r"privacy|privacidade|dpo|noreply|no-reply|suporte|support|abuse",m,re.I)]
    preferred=[m for m in matches if re.search(r"recrut|curricul|talent|selec|jobs|carreira|^rh@",m,re.I)]
    return preferred[0] if preferred else (matches[0] if len(matches)==1 else None)


def configured():
    return all(settings().get(k) for k in ["SMTP_HOST","SMTP_USER","SMTP_PASSWORD"])


def settings():
    values={k:os.environ.get(k,"") for k in ["SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASSWORD"]}
    private=Path(os.environ.get("SMTP_CONFIG_FILE","/app/data/smtp-private.json"))
    if private.is_file():
        values.update(json.loads(private.read_text(encoding="utf-8")))
    return values


def send(conn,job_id,candidate,output_dir,generate):
    if not configured():
        raise HTTPException(409,"E-mail ainda não configurado. Consulte Como ativar > E-mail.")
    config=settings()
    conn.execute("BEGIN IMMEDIATE")
    row=conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
    if not row or row["decision"]!="aprovada" or row["status"] in ("enviada","ignorada") or row["apply_state"]=="processing":
        conn.rollback()
        raise HTTPException(409,"Vaga não aprovada, já enviada ou em processamento. Nenhum e-mail enviado.")
    address=recipient(row)
    if not address:
        conn.rollback()
        raise HTTPException(400,"Não há um único e-mail de candidatura identificado")
    now=datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET apply_state='processing',apply_attempted_at=?,apply_message='Preparando e-mail ATS' WHERE id=?",(now,job_id))
    conn.commit()
    try:
        generate(conn,job_id)
        row=conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        en=row["market"]=="exterior"
        role=re.sub(r"[\r\n]"," ",row["cargo"] or row["title"])[:150]
        msg=EmailMessage()
        msg["From"]=config["SMTP_USER"]
        msg["To"]=address
        msg["Reply-To"]=candidate["email"]
        msg["Subject"]=("Application — " if en else "Candidatura — ")+role
        msg["Message-ID"]=make_msgid()
        body=(f"Hello,\n\nI would like to apply for the {role} position. Please find my tailored resume attached. I am based in Brazil and interested in remote opportunities.\n\nThank you for your consideration.\n{candidate['nome_completo']}"
              if en else f"Olá,\n\nGostaria de me candidatar à vaga de {role}. Segue em anexo meu currículo adaptado à oportunidade. Fico à disposição para conversar sobre minha experiência.\n\nAtenciosamente,\n{candidate['nome_completo']}")
        msg.set_content(body)
        root=Path(output_dir).resolve()
        pdf=(root/row["pdf_path"]).resolve()
        if root not in pdf.parents or not pdf.is_file():
            raise ValueError("Currículo não encontrado")
        content=pdf.read_bytes()
        if hashlib.sha256(content).hexdigest()!=row["pdf_sha256"]:
            raise ValueError("Currículo mudou durante a preparação")
        msg.add_attachment(content,maintype="application",subtype="pdf",filename=pdf.name)
        host=config["SMTP_HOST"]
        port=int(config.get("SMTP_PORT") or "587")
        if port==465:
            server=smtplib.SMTP_SSL(host,port,timeout=45,context=ssl.create_default_context())
        else:
            server=smtplib.SMTP(host,port,timeout=45)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        with server:
            server.login(config["SMTP_USER"],config["SMTP_PASSWORD"])
            refused=server.send_message(msg)
            if refused:
                raise ValueError("Servidor recusou o destinatário")
        note=f"E-mail aceito pelo servidor SMTP para {address}. ID: {msg['Message-ID']}. Aceitação não garante entrega/leitura."
        conn.execute("UPDATE jobs SET status='enviada',apply_state='sent',sent_via='automatic',sent_at=?,receipt_url=?,receipt_text=?,apply_message=?,updated_at=? WHERE id=?",(now,"mailto:"+address,note,note,now,job_id))
        event(conn,job_id,"sent",note)
        conn.commit()
        return {"ok":True,"recipient":address,"message":note}
    except Exception:
        note="Envio de e-mail sem confirmação. Confira sua caixa de enviados antes de tentar novamente; não haverá repetição automática."
        conn.execute("UPDATE jobs SET apply_state='action_required',status='aguardando ação humana',apply_message=? WHERE id=?",(note,job_id))
        event(conn,job_id,"action_required",note)
        conn.commit()
        raise HTTPException(502,note)
