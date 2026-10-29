import os
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch,MagicMock

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'worker'))
import app
import automation
import dashboard
import mailer
from fastapi import HTTPException
from pypdf import PdfReader


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(app,'DATA_DIR',Path(self.temp.name)),patch.object(app,'DB_PATH',Path(self.temp.name)/'jobs.db'),patch.object(app,'OUTPUT_DIR',Path(self.temp.name)/'pdf'),patch.object(app,'CONFIG_DIR',Path(__file__).resolve().parents[1]/'config')]
        for p in self.patches:p.start()
        self.secret_patch=patch.dict(os.environ,{"SMTP_CONFIG_FILE":str(Path(self.temp.name)/'smtp.json'),"PERSONALIZED_DIR":""})
        self.secret_patch.start()
        self.conn=app.get_db()
        self.job=dict(url='https://example.org/jobs/1',source='Test',title='Junior customer support',cargo='Junior customer support',company='Example',location='São Paulo',published_at=datetime.now(),salary=2000,salary_text='R$ 2000',contract_type='CLT',work_mode='Remoto',description='Customer service. Email: recrutamento@example.org')
        self.id=app.save_job(self.conn,self.job,'aprovada','teste',80)[0]
    def tearDown(self):
        self.conn.close()
        for p in reversed(self.patches):p.stop()
        self.secret_patch.stop()
        self.temp.cleanup()
    def fake_generate(self,c,id):
        c.execute("UPDATE jobs SET pdf_path='test.pdf',pdf_sha256='hash',resume_language='pt' WHERE id=?",(id,));c.commit()
    def claim(self):
        return automation.claim(self.conn,automation.VERSION,lambda r:dict(r),self.fake_generate)['job']
    def row(self):
        return self.conn.execute('SELECT * FROM jobs WHERE id=?',(self.id,)).fetchone()
    def test_remote_brazil_any_state(self):
        self.assertEqual(app.classify(self.job,app.load_json('filtros.json'))[0],'aprovada')
    def test_country_restriction(self):
        self.assertEqual(automation.eligibility(dict(location='USA only',description=''))[0],'blocked')
        self.assertEqual(automation.eligibility(dict(location='Canada',description=''))[0],'blocked')
        self.assertEqual(automation.eligibility(dict(location='Worldwide',description='Must reside in the United States'))[0],'blocked')
    def test_worldwide(self):
        self.assertEqual(automation.eligibility(dict(location='Worldwide',description=''))[0],'eligible')
        self.assertEqual(automation.eligibility(dict(location='LATAM',description=''))[0],'eligible')
        self.assertEqual(automation.eligibility(dict(location='',description='Remote role'))[0],'review')
    def test_global_feed_does_not_override_local_requirement(self):
        self.assertEqual(automation.eligibility(dict(title='Customer Engineer, Sydney',location='Anywhere in the World',description=''))[0],'blocked')
        self.assertEqual(automation.eligibility(dict(title='Support',location='Worldwide',description='Based in a European timezone (UTC to UTC+3).'))[0],'blocked')
    def test_experienced_sales_not_entry_level(self):
        job={**self.job,'market':'exterior','location':'Worldwide','title':'AI Security Sales Executive','description':'5+ years of quota-carrying sales experience'}
        self.assertEqual(app.classify(job,app.load_json('filtros.json'))[0],'descartada')
    def test_legacy_client_cannot_claim(self):
        self.assertIsNone(automation.claim(self.conn,'',lambda r:dict(r),self.fake_generate)['job'])
    def test_claim_single_and_regenerate(self):
        job=self.claim();self.assertEqual(job['pdf_sha256'],'hash')
        self.assertIsNone(automation.claim(self.conn,automation.VERSION,lambda r:dict(r),self.fake_generate)['job'])
    def test_receipt_required(self):
        job=self.claim()
        with self.assertRaises(HTTPException):automation.record_result(self.conn,self.id,app.ApplicationResult(state='sent',attempt_token=job['attempt_token']))
    def test_receipt_provenance_and_idempotence(self):
        job=self.claim();result=app.ApplicationResult(state='sent',attempt_token=job['attempt_token'],current_url='https://example.org/thanks',evidence='Application submitted',message='Confirmed')
        automation.record_result(self.conn,self.id,result);automation.record_result(self.conn,self.id,result)
        self.assertEqual(dashboard.tab_for(self.row()),'automaticas')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM application_events').fetchone()[0],1)
    def test_wrong_token(self):
        self.claim()
        with self.assertRaises(HTTPException):automation.record_result(self.conn,self.id,app.ApplicationResult(state='action_required',attempt_token='wrong'))
    def test_manual_sent_separate(self):
        self.conn.execute("UPDATE jobs SET status='enviada' WHERE id=?",(self.id,));self.conn.commit()
        self.assertEqual(dashboard.tab_for(self.row()),'enviadas')
    def test_stale_becomes_action_not_retry(self):
        self.claim();self.conn.execute('UPDATE jobs SET apply_attempted_at=?',( (datetime.now()-timedelta(hours=1)).isoformat(),));self.conn.commit();automation.recover_stale(self.conn)
        self.assertEqual(dashboard.tab_for(self.row()),'acao')
    def test_captcha_result_routes_action(self):
        job=self.claim();automation.record_result(self.conn,self.id,app.ApplicationResult(state='action_required',attempt_token=job['attempt_token'],message='CAPTCHA'))
        self.assertEqual(dashboard.tab_for(self.row()),'acao')
    def test_old_opened_url_never_attaches(self):
        self.assertIsNone(app.application_by_url(None,self.job['url'])['job'])
    def test_sensitive_question_not_answered(self):
        r=automation.answer_questions([dict(key='0',question='Are you authorized to work in the US?')],[],{},dict(enabled=False))
        self.assertIsNone(r[0]['answer'])
    def test_missing_model_does_not_fabricate(self):
        r=automation.answer_questions([dict(key='0',question='How many years of Kubernetes experience?')],[],{},dict(enabled=False))
        self.assertIsNone(r[0]['answer'])
    def test_recipient_avoids_privacy(self):
        self.assertEqual(mailer.recipient(self.row()),'recrutamento@example.org')
        self.assertIsNone(mailer.recipient(dict(description='privacy@example.org')))
    def test_smtp_disabled_no_network(self):
        with patch.dict(os.environ,{'SMTP_HOST':'','SMTP_USER':'','SMTP_PASSWORD':''}):
            with self.assertRaises(HTTPException):mailer.send(self.conn,self.id,{},app.OUTPUT_DIR,self.fake_generate)
    def test_english_resume(self):
        self.conn.execute("UPDATE jobs SET market='exterior' WHERE id=?",(self.id,));self.conn.commit()
        app.generate_pdf(self.conn,self.id)
        pdf=PdfReader(str(app.OUTPUT_DIR/self.row()['pdf_path']))
        text=''.join(p.extract_text() for p in pdf.pages)
        self.assertIn('English: intermediate',text)
        self.assertIn('PROFESSIONAL EXPERIENCE',text)
        self.assertNotIn('Inglês intermediário',text)
        self.assertEqual(len(pdf.pages),1)
    def test_regenerate_keeps_sent_status(self):
        self.conn.execute("UPDATE jobs SET status='enviada' WHERE id=?",(self.id,));self.conn.commit();app.generate_pdf(self.conn,self.id)
        self.assertEqual(self.row()['status'],'enviada')
    def test_market_ui_does_not_leak_other_market(self):
        self.conn.execute("UPDATE jobs SET market='exterior' WHERE id=?",(self.id,));self.conn.commit()
        page=dashboard.render(self.conn,{},app.workspace_config(),'brasil','fila')
        self.assertNotIn('Junior customer support</h3>',page)
    def test_email_sends_english_pdf_and_logs_acceptance(self):
        self.conn.execute("UPDATE jobs SET market='exterior' WHERE id=?",(self.id,));self.conn.commit()
        server=MagicMock(); server.__enter__.return_value=server; server.send_message.return_value={}
        env={'SMTP_HOST':'smtp.example.org','SMTP_USER':'sender@example.org','SMTP_PASSWORD':'test-only'}
        with patch.dict(os.environ,env),patch('mailer.smtplib.SMTP',return_value=server):
            mailer.send(self.conn,self.id,app.load_json('candidato.json'),app.OUTPUT_DIR,app.generate_pdf)
        message=server.send_message.call_args.args[0]
        self.assertTrue(message['Subject'].startswith('Application'))
        attachment=list(message.iter_attachments())[0]
        self.assertTrue(attachment.get_filename().endswith('.pdf'))
        self.assertEqual(self.row()['sent_via'],'automatic')
        self.assertIn('SMTP',self.row()['receipt_text'])
    def test_email_duplicate_rejected(self):
        self.conn.execute("UPDATE jobs SET status='enviada' WHERE id=?",(self.id,));self.conn.commit()
        with patch('mailer.configured',return_value=True),patch('mailer.smtplib.SMTP') as smtp:
            with self.assertRaises(HTTPException):mailer.send(self.conn,self.id,{},app.OUTPUT_DIR,self.fake_generate)
            smtp.assert_not_called()


if __name__=='__main__':unittest.main()
