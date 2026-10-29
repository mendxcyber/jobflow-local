"""One-off classification migration; never touches sent or ignored jobs."""
from datetime import datetime
import app

def run():
    conn=app.get_db()
    filters=app.load_json('filtros.json')
    counts={}
    try:
        for row in conn.execute("SELECT * FROM jobs WHERE status NOT IN ('enviada','ignorada') AND apply_state<>'processing' AND COALESCE(hidden,0)=0").fetchall():
            job=dict(row)
            job['published_at']=datetime.fromisoformat(job['published_at']) if job['published_at'] else None
            decision,reason,score=app.classify(job,filters)
            conn.execute("UPDATE jobs SET decision=?,reason=?,score=?,hidden=? WHERE id=?",(decision,reason,score,int(decision=='descartada'),row['id']))
            counts[decision]=counts.get(decision,0)+1
        conn.commit()
        print(counts)
    finally:
        conn.close()

if __name__=='__main__':run()
