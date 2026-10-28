import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'worker'))
import app

class FiltersTest(unittest.TestCase):
    def setUp(self):
        import json
        self.filters=json.loads((Path(__file__).resolve().parents[1]/'config/filtros.json').read_text(encoding='utf-8'))
    def job(self,**kwargs):
        return dict(title='Assistente de suporte',description='Suporte a usuários',work_mode='Remoto',contract_type='CLT',salary=None,salary_text='',published_at=datetime.now(),**kwargs)
    def test_pcd_exclusive_is_rejected(self):
        job=self.job();job['title']='Assistente de suporte - exclusiva PCD'
        self.assertEqual(app.classify(job,self.filters)[0],'descartada')
    def test_old_job_is_rejected(self):
        job=self.job();job['published_at']=datetime.now()-timedelta(days=10)
        self.assertEqual(app.classify(job,self.filters)[0],'descartada')
    def test_hybrid_is_rejected(self):
        job=self.job();job['work_mode']='Híbrido'
        self.assertEqual(app.classify(job,self.filters)[0],'descartada')
    def test_foreign_feed_requires_human_review(self):
        job=self.job(international_review=True)
        self.assertEqual(app.classify(job,self.filters)[0],'pendente')
    def test_unknown_feed_date_requires_review(self):
        job=self.job(source='GeekHunter');job['published_at']=None
        self.assertEqual(app.classify(job,self.filters)[0],'pendente')

if __name__=='__main__':unittest.main()
