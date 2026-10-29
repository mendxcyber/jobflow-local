import html
import mailer
from datetime import datetime, timedelta
from pathlib import Path
from automation import recover_stale, read_state

LABELS = {"novas":"Novas oportunidades", "fila":"Fila automática", "acao":"Ação necessária", "automaticas":"Currículos enviados automaticamente", "enviadas":"Enviadas / histórico", "pendentes":"Pendentes", "ignoradas":"Ignoradas"}
E = lambda value: html.escape(str(value or ""), quote=True)


def tab_for(row):
    if row["status"] == "enviada":
        return "automaticas" if row["sent_via"] == "automatic" else "enviadas"
    if row["status"] == "ignorada":
        return "ignoradas"
    if row["status"] == "aguardando ação humana":
        if row["apply_message"]=="Formulário preenchido e pronto para o envio final":
            return "novas"
        return "acao"
    if row["decision"] == "pendente":
        return "pendentes"
    # Approved work is automatic by default. Manual-ready forms are explicitly routed.
    if row["apply_state"] == "manual_ready":
        return "novas"
    return "fila"


def render(conn, sources, market, tab):
    market = market if market in {"brasil","exterior"} else "brasil"
    tab = tab if tab in LABELS else "novas"
    recover_stale(conn)
    all_rows = conn.execute("SELECT * FROM jobs WHERE COALESCE(hidden,0)=0 AND decision<>'descartada' AND market=? ORDER BY score DESC, discovered_at DESC",(market,)).fetchall()
    counts = {key:sum(tab_for(r)==key for r in all_rows) for key in LABELS}
    rows = [r for r in all_rows if tab_for(r)==tab]
    hb = read_state(conn,"extension",{})
    online = bool(hb.get("at") and datetime.fromisoformat(hb["at"]) > datetime.now()-timedelta(minutes=7))
    connected = online and hb.get("version")=="3.0.0"
    engine = "Extensão conectada" if connected else "Extensão desconectada ou desatualizada"
    detail = "Envio automático ativo" if connected and hb.get("autoSubmitEnabled") and hb.get("autoApplyEnabled") else "Instale/atualize a extensão 3.0.0 e confira as opções. O painel não envia sozinho."
    unread = conn.execute("SELECT count(*) FROM application_events WHERE state='sent' AND seen=0").fetchone()[0]
    events = conn.execute("SELECT e.*,j.cargo,j.title FROM application_events e JOIN jobs j ON j.id=e.job_id WHERE e.state='sent' AND j.market=? ORDER BY e.id DESC LIMIT 5",(market,)).fetchall()
    source_errors = read_state(conn,"last_search",{})
    market_nav = ''.join(f'<a class="market {"selected" if key==market else ""}" href="/?market={key}&tab=novas">{label}</a>' for key,label in [("brasil","Brasil"),("exterior","EUA / Canadá")])
    nav = ''.join(f'<a class="tab {"active" if key==tab else ""}" href="/?market={market}&tab={key}">{label}<b>{counts[key]}</b></a>' for key,label in LABELS.items())
    manual_sources = sources.get("buscas_manuais",[]) if market=="brasil" else sources.get("exterior_manuais",[])
    links = ''.join(f'<a class="source" href="{E(s["url"])}" target="_blank" rel="noopener">{E(s["nome"])} ↗</a>' for s in manual_sources)
    cards=[]
    for r in rows:
        role = r["cargo"] or r["title"]
        salary = (f'R$ {r["salary"]:,.2f}' if r["salary"] else "Salário no anúncio") if market=="brasil" else "Moeda e período no anúncio"
        base=f'market={market}&return_tab={tab}'
        actions=f'<a class="button" target="_blank" rel="noopener" href="{E(r["url"])}">{"Resolver etapa" if tab=="acao" else "Abrir vaga"} ↗</a>'
        if r["pdf_path"]:
            actions+=f'<a class="button secondary" href="/pdf/{r["id"]}">Currículo ATS ↓</a>'
        if tab in {"novas","fila","acao"} and r["decision"]=="aprovada":
            actions+=f'<form method="post" action="/jobs/{r["id"]}/regenerate?return_tab={tab}&market={market}"><button class="quiet">Regenerar ATS</button></form>'
            address=mailer.recipient(r)
            if address and r["apply_state"]!="processing":
                actions+=f'<form method="post" action="/jobs/{r["id"]}/email?market={market}"><button class="button secondary" {"" if mailer.configured() else "disabled"}>Enviar por e-mail</button><small class="recipient">Para: {E(address)}</small></form>'
                if not mailer.configured():
                    actions+='<a class="quiet" href="/settings/email">Configurar conta de envio</a>'
        if tab in {"novas","acao","fila"}:
            actions+=f'<form action="/jobs/{r["id"]}/status?{base}" method="post"><input type="hidden" name="status" value="enviada"><button class="button secondary">Marcar envio manual</button></form>'
        if tab not in {"enviadas","automaticas","ignoradas"}:
            actions+=f'<form action="/jobs/{r["id"]}/status?{base}" method="post"><input type="hidden" name="status" value="ignorada"><button class="quiet">Ignorar</button></form>'
        if tab=="pendentes":
            actions+=f'<form action="/jobs/{r["id"]}/approve?market={market}" method="post"><button class="button secondary">Aprovar após revisão</button></form>'
        if tab=="acao":
            actions+=f'<form action="/jobs/{r["id"]}/restore?market={market}" method="post"><button class="quiet">Reenfileirar após conferir</button></form>'
        receipt=""
        if tab=="automaticas":
            receipt=f'<div class="receipt">✓ Envio confirmado · {E(r["sent_at"])}<br><a href="{E(r["receipt_url"])}" target="_blank" rel="noopener">Ver página de confirmação ↗</a><p>{E(r["receipt_text"])}</p><small>PDF SHA-256: {E(r["pdf_sha256"])}</small></div>'
        state = "Processando" if r["apply_state"]=="processing" else LABELS[tab]
        reason = r["apply_message"] or r["reason"]
        cards.append(f'<article class="job"><div class="job-top"><span class="tag">{E(r["source"])}</span><span class="state">{E(state)}</span></div><h3>{E(role)}</h3><p class="company">{E(r["company"] or "Empresa no anúncio")}</p><div class="metadata"><span>⌖ {E(r["location"])}</span><span>◷ {E(r["work_mode"])}</span><span>{E(salary)}</span><span>{E(r["contract_type"])}</span></div><p class="reason">{E(reason)}</p>{receipt}<footer>{actions}</footer></article>')
    empty='<div class="empty"><span>◎</span><h3>Nenhuma oportunidade nesta etapa</h3><p>Vagas aprovadas seguem para a fila automática. Etapas bloqueadas aparecem em Ação necessária.</p><a href="/?market='+market+'&tab=fila">Ver fila automática →</a></div>'
    alerts=''.join(f'<li><strong>{E(e["cargo"] or e["title"])}</strong><span>{E(e["created_at"])}</span></li>' for e in events) or '<li>Ainda não há envio automático confirmado neste mercado.</li>'
    errors=''.join(f'<li>{E(x.get("fonte"))}: {E(x.get("erro") or x.get("aviso"))}</li>' for x in (source_errors.get("erros",[])+source_errors.get("avisos",[]))[:10])
    css=Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")
    subtitle="Remotas em todo o Brasil · presencial conforme seus filtros" if market=="brasil" else "Mercado internacional · somente remoto a partir do Brasil; elegibilidade ambígua exige revisão"
    return f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>JobFlow · Central de candidaturas</title><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2048%2048%22%20fill%3D%22none%22%3E%3Cpath%20d%3D%22M11%208h24l-7%208H18l-7%208H3L17%208%22%20fill%3D%22%2372e6d1%22%2F%3E%3Cpath%20d%3D%22M37%2040H13l7-8h10l7-8h8L31%2040%22%20fill%3D%22%2372e6d1%22%2F%3E%3Cpath%20d%3D%22m18%2020%208-9%206%206-14%2016-6-6%206-7Z%22%20fill%3D%22%2372e6d1%22%20opacity%3D%22.45%22%2F%3E%3C%2Fsvg%3E"><style>{css}</style></head>
    <body><aside><a class="brand" href="/" aria-label="JobFlow"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" fill="none"><path d="M11 8h24l-7 8H18l-7 8H3L17 8" fill="#72e6d1"/><path d="M37 40H13l7-8h10l7-8h8L31 40" fill="#72e6d1"/><path d="m18 20 8-9 6 6-14 16-6-6 6-7Z" fill="#72e6d1" opacity=".45"/></svg><span>JobFlow</span></a><div class="side-caption">LOCAL / OPEN SOURCE</div><nav>{market_nav}</nav><div class="side-bottom"><span class="dot {"on" if connected else ""}"></span>{E(engine)}<small>{E(detail)}</small><a href="https://github.com/mendxcyber/jobflow-browser-extension">Código da extensão ↗</a><a href="/guide">Como ativar →</a></div></aside>
    <main><header><div><span class="eyebrow">CENTRAL DE OPORTUNIDADES</span><h1>Seu próximo passo começa aqui<span>.</span></h1><p>{subtitle}</p></div><form method="post" action="/run-browser?market={market}"><button class="button primary">＋ Pesquisar agora</button></form></header>
    <section class="stats"><div><small>EM AUTOMAÇÃO</small><strong>{counts['fila']:02}</strong><span>Preparação e envio pela extensão</span></div><div><small>PRECISAM DE VOCÊ</small><strong>{counts['acao']+counts['novas']:02}</strong><span>Confirmações e etapas manuais</span></div><div><small>ENVIOS AUTOMÁTICOS</small><strong>{counts['automaticas']:02}</strong><span>Com confirmação e currículo registrado</span></div></section>
    <div class="statusbar"><span class="dot {"on" if connected else ""}"></span><strong>{E(engine)}</strong><span>{E(detail)}</span></div>
    <section class="sources"><span>EXPLORAR FONTES</span>{links}</section><nav class="tabs" aria-label="Etapas das candidaturas">{nav}</nav>
    <div class="section-title"><h2>{LABELS[tab]}</h2><span>{len(rows)} registros · atualização ao recarregar</span></div><section class="jobs">{''.join(cards) or empty}</section>
    <details class="activity"><summary>Notificações de envios · {unread} não lidas</summary><ul>{alerts}</ul><form method="post" action="/api/notifications/read"><button class="quiet">Marcar como lidas</button></form></details>
    <details class="activity"><summary>Diagnóstico de coleta e automação</summary><p>Último contato: {E(hb.get('at','nenhum cliente 3.0.0 conectado'))}. O n8n pesquisa; a extensão executa os formulários no navegador.</p><ul>{errors or '<li>Sem erros de coleta registrados nesta versão.</li>'}</ul></details></main>
    <script>let last=localStorage.getItem('jobflow-event')||'0';async function poll(){{try{{let r=await fetch('/api/notifications');let d=await r.json();let n=d.events.filter(e=>e.state==='sent'&&e.id>Number(last));if(n.length){{let t=document.createElement('div');t.className='toast';t.textContent=n.length+' currículo(s) enviado(s) automaticamente. Confira o histórico.';document.body.append(t);setTimeout(()=>t.remove(),12000)}}if(d.latest_id){{last=String(d.latest_id);localStorage.setItem('jobflow-event',last)}}}}catch(e){{}}}}poll();setInterval(poll,30000);</script></body></html>'''

