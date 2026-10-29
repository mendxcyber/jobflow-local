import html
import mailer
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from automation import VERSION, recover_stale, read_state

LABELS = {"novas":"Novas oportunidades", "fila":"Fila automática", "acao":"Ação necessária", "automaticas":"Currículos enviados automaticamente", "enviadas":"Enviadas / histórico", "pendentes":"Pendentes", "ignoradas":"Ignoradas"}
E = lambda value: html.escape(str(value or ""), quote=True)


def tab_for(row):
    if row["status"] == "enviada":
        return "automaticas" if row["sent_via"] == "automatic" else "enviadas"
    if row["status"] == "ignorada":
        return "ignoradas"
    if row["status"] == "aguardando ação humana":
        return "novas" if row["apply_message"] == "Formulário preenchido e pronto para o envio final" else "acao"
    if row["decision"] == "pendente":
        return "pendentes"
    # Approved work is automatic by default. Manual-ready forms are explicitly routed.
    if row["apply_state"] == "manual_ready":
        return "novas"
    return "fila"


def _workspaces(config):
    values = config.get("workspaces", [])
    return values or [{"id":"brasil","label":"Brasil"},{"id":"exterior","label":"EUA / Canadá"}]


def render(conn, sources, workspace_config, market, tab, q=""):
    workspaces = _workspaces(workspace_config)
    ids = {x.get("id") for x in workspaces}
    market = market if market in ids else workspaces[0]["id"]
    tab = tab if tab in LABELS else "novas"
    recover_stale(conn)
    all_rows = conn.execute("SELECT * FROM jobs WHERE COALESCE(hidden,0)=0 AND decision<>'descartada' AND market=? ORDER BY score DESC, discovered_at DESC",(market,)).fetchall()
    query = (q or "").strip().lower()
    counts = {key:sum(tab_for(r)==key for r in all_rows) for key in LABELS}
    rows = [r for r in all_rows if tab_for(r)==tab]
    if query:
        rows = [r for r in rows if query in " ".join(str(r[x] or "") for x in ("title","cargo","company","source","description","location")).lower()]
    hb = read_state(conn,"extension",{})
    online = bool(hb.get("at") and datetime.fromisoformat(hb["at"]) > datetime.now()-timedelta(minutes=7))
    connected = online and hb.get("version")==VERSION
    engine = "Extensão conectada" if connected else "Extensão desconectada ou desatualizada"
    detail = "Envio automático ativo" if connected and hb.get("autoSubmitEnabled") and hb.get("autoApplyEnabled") else f"Instale/atualize a extensão {VERSION} e confira as opções."
    unread = conn.execute("SELECT count(*) FROM application_events WHERE state='sent' AND seen=0").fetchone()[0]
    events = conn.execute("SELECT e.*,j.cargo,j.title FROM application_events e JOIN jobs j ON j.id=e.job_id WHERE e.state='sent' AND j.market=? ORDER BY e.id DESC LIMIT 5",(market,)).fetchall()
    source_errors = read_state(conn,"last_search",{})
    market_nav = ''.join(f'<a class="market {"selected" if x.get("id")==market else ""}" href="/?market={E(x.get("id"))}&tab=novas">{E(x.get("label"))}</a>' for x in workspaces)
    nav = ''.join(f'<a class="tab {"active" if key==tab else ""}" href="/?market={E(market)}&tab={key}">{label}<b>{counts[key]}</b></a>' for key,label in LABELS.items())
    workspace = next(x for x in workspaces if x.get("id")==market)
    manual_sources = list(workspace.get("manual_sources", []))
    legacy_key = workspace.get("manual_sources_key")
    if legacy_key:
        manual_sources = sources.get(legacy_key, []) + manual_sources
    links = ''.join(f'<a class="source" href="{E(s["url"])}" target="_blank" rel="noopener">{E(s["nome"])} ↗</a>' for s in manual_sources)
    cards=[]
    for r in rows:
        role = r["cargo"] or r["title"]
        salary = (f'R$ {r["salary"]:,.2f}' if r["salary"] else "Salário no anúncio") if market=="brasil" else "Moeda e período no anúncio"
        base=f'market={quote(market)}&return_tab={tab}'
        actions=f'<a class="button" target="_blank" rel="noopener" href="{E(r["url"])}">{"Resolver etapa" if tab=="acao" else "Abrir vaga"} ↗</a>'
        if r["pdf_path"]:
            filename=Path(r["pdf_path"]).name
            actions+=f'<button class="button secondary" type="button" onclick="saveResume({r["id"]},\'{E(filename)}\')">Salvar currículo ATS ↓</button>'
        if tab in {"novas","fila","acao"} and r["decision"]=="aprovada":
            actions+=f'<form method="post" action="/jobs/{r["id"]}/regenerate?return_tab={tab}&market={E(market)}"><button class="quiet">Regenerar ATS</button></form>'
            address=mailer.recipient(r)
            if address and r["apply_state"]!="processing":
                actions+=f'<form method="post" action="/jobs/{r["id"]}/email?market={E(market)}"><button class="button secondary" {"" if mailer.configured() else "disabled"}>Enviar por e-mail</button><small class="recipient">Para: {E(address)}</small></form>'
        if tab in {"novas","acao","fila"}:
            actions+=f'<form action="/jobs/{r["id"]}/status?{base}" method="post"><input type="hidden" name="status" value="enviada"><button class="button secondary">Marcar envio manual</button></form>'
        if tab not in {"enviadas","automaticas","ignoradas"}:
            actions+=f'<form action="/jobs/{r["id"]}/status?{base}" method="post"><input type="hidden" name="status" value="ignorada"><button class="quiet">Ignorar</button></form>'
        if tab=="pendentes":
            actions+=f'<form action="/jobs/{r["id"]}/approve?market={E(market)}" method="post"><button class="button secondary">Aprovar após revisão</button></form>'
        if tab=="acao":
            actions+=f'<form action="/jobs/{r["id"]}/restore?market={E(market)}" method="post"><button class="quiet">Reenfileirar após conferir</button></form>'
        receipt=""
        if tab=="automaticas":
            receipt=f'<div class="receipt">✓ Envio confirmado · {E(r["sent_at"])}<br><a href="{E(r["receipt_url"])}" target="_blank" rel="noopener">Ver página de confirmação ↗</a><p>{E(r["receipt_text"])}</p><small>PDF SHA-256: {E(r["pdf_sha256"])}</small></div>'
        state = "Processando" if r["apply_state"]=="processing" else LABELS[tab]
        reason = r["apply_message"] or r["reason"]
        score=max(0,min(100,int(r["score"] or 0)))
        adherence=f'<div class="adherence"><span>Aderência ao currículo e filtros</span><strong>{score}%</strong><i><b style="width:{score}%"></b></i></div>'
        cards.append(f'<article class="job"><div class="job-top"><span class="tag">{E(r["source"])}</span><span class="state">{E(state)}</span></div><h3>{E(role)}</h3><p class="company">{E(r["company"] or "Empresa no anúncio")}</p>{adherence}<div class="metadata"><span>⌖ {E(r["location"])}</span><span>◷ {E(r["work_mode"])}</span><span>{E(salary)}</span><span>{E(r["contract_type"])}</span></div><p class="reason">{E(reason)}</p>{receipt}<footer>{actions}</footer></article>')
    empty='<div class="empty"><span>◎</span><h3>Nenhuma oportunidade nesta etapa</h3><p>Vagas aprovadas seguem para a fila automática. Etapas bloqueadas aparecem em Ação necessária.</p></div>'
    alerts=''.join(f'<li><strong>{E(e["cargo"] or e["title"])}</strong><span>{E(e["created_at"])}</span></li>' for e in events) or '<li>Ainda não há envio automático confirmado neste mercado.</li>'
    errors=''.join(f'<li>{E(x.get("fonte"))}: {E(x.get("erro") or x.get("aviso"))}</li>' for x in (source_errors.get("erros",[])+source_errors.get("avisos",[]))[:10])
    css=Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")
    subtitle="Remotas em todo o Brasil · presencial conforme seus filtros" if market=="brasil" else f'{E(workspace.get("label"))} · regras configuráveis pela IA local'
    return f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>JobFlow · Central de candidaturas</title><style>{css}</style></head>
    <body><div id="splash"><img src="/assets/jobflow-banner.png" alt="JobFlow"><div class="splash-line"></div></div><aside><a class="brand" href="/" aria-label="JobFlow"><img src="/assets/jobflow-icon.svg" alt=""><span>JobFlow</span></a><div class="side-caption">WORKSPACES</div><nav>{market_nav}</nav><div class="side-bottom"><span class="dot {"on" if connected else ""}"></span>{E(engine)}<small>{E(detail)}</small><a href="/assistant">Configurar com IA local →</a><a href="/downloads/JobFlow-{VERSION}.xpi">Instalar extensão ↓</a><a href="/guide">Como ativar →</a></div></aside>
    <main><header><div><span class="eyebrow">CENTRAL DE OPORTUNIDADES</span><h1>Seu próximo passo começa aqui<span>.</span></h1><p>{subtitle}</p></div><form method="post" action="/run-browser?market={E(market)}"><button class="button primary">＋ Pesquisar agora</button></form></header>
    <form class="search" method="get"><input type="hidden" name="market" value="{E(market)}"><input type="hidden" name="tab" value="{E(tab)}"><input name="q" value="{E(q)}" placeholder="Pesquisar cargo, empresa, fonte, local ou palavra-chave"><button class="button">Pesquisar</button>{'<a class="quiet" href="/?market='+E(market)+'&tab='+E(tab)+'">Limpar</a>' if q else ''}</form>
    <section class="stats"><div><small>EM AUTOMAÇÃO</small><strong>{counts['fila']:02}</strong><span>Preparação e envio pela extensão</span></div><div><small>PRECISAM DE VOCÊ</small><strong>{counts['acao']+counts['novas']:02}</strong><span>Confirmações e etapas manuais</span></div><div><small>ENVIOS AUTOMÁTICOS</small><strong>{counts['automaticas']:02}</strong><span>Com confirmação e currículo registrado</span></div></section>
    <div class="statusbar"><span class="dot {"on" if connected else ""}"></span><strong>{E(engine)}</strong><span>{E(detail)}</span></div>
    <section class="sources"><span>EXPLORAR FONTES</span>{links}</section><nav class="tabs">{nav}</nav>
    <div class="section-title"><h2>{LABELS[tab]}</h2><span>{len(rows)} registros</span></div><section class="jobs">{''.join(cards) or empty}</section>
    <details class="activity"><summary>Notificações de envios · {unread} não lidas</summary><ul>{alerts}</ul><form method="post" action="/api/notifications/read"><button class="quiet">Marcar como lidas</button></form></details>
    <details class="activity"><summary>Diagnóstico de coleta e automação</summary><p>Último contato: {E(hb.get('at','nenhum cliente conectado'))}. O n8n pesquisa; a extensão executa os formulários.</p><ul>{errors or '<li>Sem erros de coleta registrados.</li>'}</ul></details></main>
    <script>
    if(sessionStorage.getItem('jobflow-splash'))document.getElementById('splash').remove();else{{sessionStorage.setItem('jobflow-splash','1');setTimeout(()=>document.getElementById('splash')?.classList.add('done'),1800);setTimeout(()=>document.getElementById('splash')?.remove(),2500)}}
    const db=()=>new Promise((ok,no)=>{{let r=indexedDB.open('jobflow',1);r.onupgradeneeded=()=>r.result.createObjectStore('handles');r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)}});
    async function saveResume(id,name){{let blob=await fetch('/pdf/'+id).then(r=>{{if(!r.ok)throw Error('PDF indisponível');return r.blob()}});if(window.showDirectoryPicker){{let d=await db(),tx=d.transaction('handles','readwrite'),store=tx.objectStore('handles'),handle=await new Promise(ok=>{{let x=store.get('resumeDir');x.onsuccess=()=>ok(x.result)}});if(handle&&!confirm('Salvar novamente na pasta escolhida anteriormente?'))handle=null;if(!handle){{handle=await showDirectoryPicker({{mode:'readwrite'}});store.put(handle,'resumeDir')}}if((await handle.requestPermission({{mode:'readwrite'}}))!=='granted')throw Error('Permissão negada');let f=await handle.getFileHandle(name,{{create:true}}),w=await f.createWritable();await w.write(blob);await w.close();alert('Currículo salvo em '+handle.name);return}}let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),30000);alert('Seu navegador controla a pasta. No Firefox, ative “Sempre perguntar onde salvar”.')}}
    let last=localStorage.getItem('jobflow-event')||'0';async function poll(){{try{{let d=await fetch('/api/notifications').then(r=>r.json()),n=d.events.filter(e=>e.state==='sent'&&e.id>Number(last));if(n.length){{let t=document.createElement('div');t.className='toast';t.textContent=n.length+' currículo(s) enviado(s) automaticamente.';document.body.append(t);setTimeout(()=>t.remove(),12000)}}if(d.latest_id){{last=String(d.latest_id);localStorage.setItem('jobflow-event',last)}}}}catch(e){{}}}}poll();setInterval(poll,30000);
    </script></body></html>'''
