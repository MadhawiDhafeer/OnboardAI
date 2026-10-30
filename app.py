import streamlit as st
import sqlite3, json, os, re, urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
import pandas as pd

st.set_page_config(page_title='OnboardAI | Autonomous HR', page_icon='🚀', layout='wide')
DB = Path(os.getenv('ONBOARDAI_DB', 'onboardai.db'))
DEPARTMENTS = ['Technology', 'Data & Analytics', 'Sales', 'Finance', 'Human Resources', 'Operations']
REQUIRED_DOCS = ['National ID', 'Signed contract', 'Bank details']

@st.cache_resource
def connect():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT, department TEXT, manager TEXT, start_date TEXT, documents TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, employee_id INTEGER, title TEXT, owner TEXT, status TEXT, details TEXT, requires_approval INTEGER DEFAULT 0, UNIQUE(employee_id,title));
    CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, employee_id INTEGER, timestamp TEXT, action TEXT, outcome TEXT);
    CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, employee_id INTEGER, recipient TEXT, subject TEXT, body TEXT, status TEXT, UNIQUE(employee_id,subject));
    ''')
    conn.commit()
    return conn

def execute(sql, params=()):
    db = connect()
    with db:
        cur = db.execute(sql, params)
    return cur

def rows(sql, params=()):
    return [dict(r) for r in connect().execute(sql, params).fetchall()]

def log(eid, action, outcome):
    execute('INSERT INTO events(employee_id,timestamp,action,outcome) VALUES(?,?,?,?)', (eid,datetime.now().isoformat(timespec='seconds'),action,outcome))

def task(eid, title, owner, status='Pending', details='', approval=False):
    execute('INSERT OR IGNORE INTO tasks(employee_id,title,owner,status,details,requires_approval) VALUES(?,?,?,?,?,?)', (eid,title,owner,status,details,int(approval)))

def llm_plan(employee):
    try:
        key = st.secrets.get('GEMINI_API_KEY', os.getenv('GEMINI_API_KEY', ''))
    except Exception:
        key = os.getenv('GEMINI_API_KEY', '')
    if not key:
        return None, 'Rule-based planner (no API key configured)'
    try:
        model = st.secrets.get('GEMINI_MODEL', 'gemini-2.0-flash')
    except Exception:
        model = 'gemini-2.0-flash'
    prompt = f'''You are an HR onboarding planning assistant. Employee: role={employee['role']}, department={employee['department']}. Return ONLY valid JSON with keys "tasks" (array of 2 to 3 objects each with "title", "owner" and "details"), and "welcome_note" (one short professional sentence). Owners must be HR, IT, Manager, or Employee. Tasks must be practical, specific to the role, and NOT duplicate general ID verification, welcome email, laptop, IT access, or orientation. No confidential data. Do not claim tasks were executed.'''
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    payload = json.dumps({'contents':[{'parts':[{'text':prompt}]}], 'generationConfig':{'responseMimeType':'application/json','temperature':0.2}}).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={'Content-Type':'application/json','x-goog-api-key':key}, method='POST')
        with urllib.request.urlopen(req, timeout=12) as resp:
            result=json.load(resp)
        raw=result['candidates'][0]['content']['parts'][0]['text']
        parsed=json.loads(raw)
        if not isinstance(parsed.get('tasks'),list): raise ValueError('Invalid tasks')
        return parsed, f'Gemini ({model})'
    except Exception as exc:
        return None, f'Rule-based fallback (AI unavailable: {type(exc).__name__})'

def run_agent(eid):
    employee=rows('SELECT * FROM employees WHERE id=?',(eid,))[0]
    docs=set(json.loads(employee['documents']))
    missing=[d for d in REQUIRED_DOCS if d not in docs]
    log(eid,'OBSERVE','Read employee record and document checklist')
    plan, engine=llm_plan(employee)
    log(eid,'REASON',f'{engine}; missing documents: {", ".join(missing) if missing else "none"}')
    task(eid,'Verify required documents','HR', 'Blocked' if missing else 'Completed', 'Missing: '+', '.join(missing) if missing else 'All required documents provided')
    execute('UPDATE tasks SET status=?,details=? WHERE employee_id=? AND title=?',('Blocked' if missing else 'Completed','Missing: '+', '.join(missing) if missing else 'All required documents provided',eid,'Verify required documents'))
    task(eid,'Prepare laptop and equipment','IT',details='Prepare equipment for '+employee['role'])
    task(eid,'Approve system access','IT',status='Awaiting approval',details='Manager must authorize access; no accounts are automatically provisioned',approval=True)
    task(eid,'Schedule orientation','HR',details='Arrange first-day orientation and manager introduction')
    if missing:
        task(eid,'Request missing documents','HR',status='Blocked',details=', '.join(missing))
        execute('UPDATE tasks SET status=?,details=? WHERE employee_id=? AND title=?',('Blocked',', '.join(missing),eid,'Request missing documents'))
        log(eid,'ADAPT','Created missing-document follow-up; onboarding cannot complete')
    else:
        execute('UPDATE tasks SET status=?,details=? WHERE employee_id=? AND title=?',('Completed','All required documents now available',eid,'Request missing documents'))
    custom = plan.get('tasks',[]) if plan else []
    if not custom:
        specialized={'Technology':'Set up developer environment','Data & Analytics':'Prepare analytics workspace','Sales':'Assign CRM training','Finance':'Schedule finance compliance training','Human Resources':'Review HR policies','Operations':'Prepare operations playbook'}
        custom=[{'title':specialized.get(employee['department'],'Prepare role-specific training'),'owner':'Manager','details':f'Role-specific onboarding for {employee["role"]}'}]
    for item in custom[:3]:
        if isinstance(item,dict):
            title=str(item.get('title','Role-specific training'))[:100]
            owner=item.get('owner','Manager')
            task(eid,title,owner if owner in ['HR','IT','Manager','Employee'] else 'Manager',details=str(item.get('details',''))[:500])
    welcome=(plan.get('welcome_note','') if plan else '') or f'We are excited to welcome you to the {employee["department"]} team.'
    body=f'Hello {employee["name"]},\n\n{welcome}\nYour start date is {employee["start_date"]}. Your manager is {employee["manager"]}.\n\nBest regards,\nHR Team'
    execute('INSERT OR IGNORE INTO messages(employee_id,recipient,subject,body,status) VALUES(?,?,?,?,?)',(eid,employee['email'],f'Welcome to the team, {employee["name"]}!',body,'Draft (not sent)'))
    log(eid,'ACT','Created or refreshed onboarding tasks and saved welcome email draft')
    statuses=[r['status'] for r in rows('SELECT status FROM tasks WHERE employee_id=?',(eid,))]
    log(eid,'VERIFY',f'{statuses.count("Completed")} completed, {statuses.count("Blocked")} blocked, {statuses.count("Awaiting approval")} awaiting approval')
    return engine

st.markdown('''<style>.block-container{padding-top:1.5rem}.hero{padding:24px;border-radius:18px;background:linear-gradient(120deg,#112746,#2557a6);color:white;margin-bottom:20px}.hero h1{color:white;margin:0}.small{opacity:.85}</style>''',unsafe_allow_html=True)
st.markdown('<div class="hero"><h1>🚀 OnboardAI</h1><div class="small">Autonomous employee onboarding • Observe → Reason → Act → Verify</div></div>',unsafe_allow_html=True)
page=st.sidebar.radio('Navigation',['🏠 Dashboard','➕ New Employee','🤖 Agent Workspace','📊 Impact & Report'])
st.sidebar.caption('Hackathon demo • Local SQLite storage • Emails and access are simulated')
employees=rows('SELECT * FROM employees ORDER BY id DESC')
if page=='➕ New Employee':
    st.header('Register a new employee')
    with st.form('new'):
        c1,c2=st.columns(2)
        name=c1.text_input('Full name','Sara Ahmed')
        email=c2.text_input('Work email','sara@example.com')
        role=c1.text_input('Job title','Data Analyst')
        department=c2.selectbox('Department',DEPARTMENTS,index=1)
        manager=c1.text_input('Manager','Mohammed Ali')
        start=c2.date_input('Start date',date.today()+timedelta(days=14))
        docs=st.multiselect('Documents received',REQUIRED_DOCS,default=['National ID','Bank details'])
        submitted=st.form_submit_button('Create employee and run agent',type='primary')
    if submitted:
        if not name.strip() or not role.strip() or not manager.strip() or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$',email):
            st.error('Please enter a name, role, manager, and valid email.')
        else:
            eid=execute('INSERT INTO employees(name,email,role,department,manager,start_date,documents,created_at) VALUES(?,?,?,?,?,?,?,?)',(name.strip(),email.strip(),role.strip(),department,manager.strip(),str(start),json.dumps(docs),datetime.now().isoformat(timespec='seconds'))).lastrowid
            engine=run_agent(eid)
            st.success(f'Employee #{eid} created. Agent completed its first run using {engine}. Open Agent Workspace to inspect actions.')
elif page=='🏠 Dashboard':
    st.header('Operations overview')
    tasks=rows('SELECT * FROM tasks')
    a,b,c,d=st.columns(4)
    a.metric('Employees',len(employees));b.metric('Tasks created',len(tasks));c.metric('Completed tasks',sum(t['status']=='Completed' for t in tasks));d.metric('Exceptions',sum(t['status']=='Blocked' for t in tasks))
    st.subheader('Employee onboarding pipeline')
    if employees:
        display=[]
        for e in employees:
            t=rows('SELECT status FROM tasks WHERE employee_id=?',(e['id'],))
            display.append({'Employee':e['name'],'Role':e['role'],'Department':e['department'],'Progress':f'{sum(x["status"]=="Completed" for x in t)}/{len(t)}','Blocked':sum(x['status']=='Blocked' for x in t)})
        st.dataframe(pd.DataFrame(display),hide_index=True,use_container_width=True)
    else: st.info('Start by creating an employee under New Employee.')
elif page=='🤖 Agent Workspace':
    st.header('Agent workspace')
    if not employees: st.info('Create an employee first.')
    else:
        opts={f'#{e["id"]} — {e["name"]} ({e["department"]})':e['id'] for e in employees}
        eid=st.selectbox('Select employee',opts,format_func=lambda x:x)
        eid=opts[eid]; employee=rows('SELECT * FROM employees WHERE id=?',(eid,))[0]
        x,y=st.columns([2,1])
        with x:
            st.subheader('Autonomous workflow')
            if st.button('🔄 Re-run agent',type='primary'):
                st.success('Agent executed: '+run_agent(eid))
            tasks=rows('SELECT * FROM tasks WHERE employee_id=? ORDER BY id',(eid,))
            st.dataframe(pd.DataFrame(tasks)[['title','owner','status','details']] if tasks else pd.DataFrame(),hide_index=True,use_container_width=True)
            st.subheader('Human-in-the-loop controls')
            actionable=[t for t in tasks if t['status'] in ['Pending','Awaiting approval']]
            if actionable:
                chosen=st.selectbox('Task to complete / approve',{f'#{t["id"]} {t["title"]}':t['id'] for t in actionable})
                if st.button('Confirm completion / approval'):
                    tid=int(chosen.split(' ')[0][1:]); execute('UPDATE tasks SET status=? WHERE id=?',('Completed',tid));log(eid,'HUMAN APPROVAL',f'Task #{tid} completed after human confirmation');st.rerun()
            else: st.caption('No tasks awaiting human action.')
        with y:
            st.subheader('Document checker')
            current=json.loads(employee['documents'])
            selected=st.multiselect('Documents received',REQUIRED_DOCS,default=current,key=f'docs_{eid}')
            if st.button('Save documents & re-evaluate'):
                execute('UPDATE employees SET documents=? WHERE id=?',(json.dumps(selected),eid));run_agent(eid);st.rerun()
            missing=set(REQUIRED_DOCS)-set(current)
            if missing: st.error('Missing: '+', '.join(sorted(missing)))
            else: st.success('All required documents present')
        st.subheader('Agent activity trace')
        events=rows('SELECT timestamp,action,outcome FROM events WHERE employee_id=? ORDER BY id DESC LIMIT 25',(eid,))
        st.dataframe(pd.DataFrame(events),hide_index=True,use_container_width=True)
        st.subheader('Generated email (draft only)')
        messages=rows('SELECT * FROM messages WHERE employee_id=?',(eid,))
        for m in messages:
            with st.expander(m['subject'],expanded=True):
                st.caption(f'To: {m["recipient"]} • {m["status"]}')
                st.text(m['body'])
elif page=='📊 Impact & Report':
    st.header('Verification and impact')
    if not employees: st.info('Create an employee first.')
    else:
        opts={f'#{e["id"]} — {e["name"]}':e['id'] for e in employees}
        eid=opts[st.selectbox('Employee',opts)]
        employee=rows('SELECT * FROM employees WHERE id=?',(eid,))[0]
        tasks=rows('SELECT * FROM tasks WHERE employee_id=?',(eid,))
        completed=sum(t['status']=='Completed' for t in tasks)
        percentage=round(100*completed/len(tasks)) if tasks else 0
        st.metric('Verified task completion',f'{percentage}%')
        st.progress(percentage/100)
        st.bar_chart(pd.Series(pd.DataFrame(tasks)['status'].value_counts(),name='Tasks'))
        report={'project':'OnboardAI','employee':employee['name'],'department':employee['department'],'role':employee['role'],'start_date':employee['start_date'],'completion_percent':percentage,'status':'Complete' if percentage==100 else 'In progress / requires attention','tasks':[{k:t[k] for k in ['title','owner','status','details']} for t in tasks],'events':rows('SELECT timestamp,action,outcome FROM events WHERE employee_id=? ORDER BY id',(eid,)),'note':'Simulated enterprise execution. Emails are drafts; IT access is not provisioned.'}
        st.download_button('⬇️ Download verification report',json.dumps(report,indent=2),'onboardai_report.json','application/json')
        st.caption('Impact metrics reflect actual prototype task records, not independently measured company time savings.')
