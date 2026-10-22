"""Regression checks. Run: python tests_regression.py

No network: every check is local except the config-guard subprocesses,
which use a dummy token and never reach a real API.
"""
import importlib.util, json, os, pathlib, sqlite3, subprocess, sys, tempfile, yaml
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import store
spec=importlib.util.spec_from_file_location("js",str(pathlib.Path(__file__).parent/"jobsweep.py"))
js=importlib.util.module_from_spec(spec); sys.modules["js"]=js; spec.loader.exec_module(js)
# A committed fixture, so the suite runs on a fresh clone and asserts the
# CODE rather than whatever search terms the user happens to have configured.
HERE = pathlib.Path(__file__).parent
cfg = yaml.safe_load(open(HERE / "config.test.yaml"))
S = cfg["scoring"]
P=F=0
def check(name, cond, detail=""):
    global P,F
    if cond: P+=1; print(f"  PASS  {name}")
    else:    F+=1; print(f"  FAIL  {name}  {detail}")

def score(title, **kw):
    j=js.Job(uid="x",title=title,company="C",location=kw.pop("location","Remote"),
             url="",ats="t",**kw)
    js.score_job(j,S); return j

print("#1 word-boundary matching")
for t in ["Director, International SEO","Head of International Organic Search",
          "VP, Internal Communications"]:
    check(f'"{t}" not excluded', score(t, arrangement="Remote OK").score > 0)
check('"Geotechnical Engineer" not a geo match',
      "geo" not in " ".join(score("Geotechnical Engineer").reasons))
check('"Senior SEO Manager" still scores', score("Senior SEO Manager", arrangement="Remote OK").score >= 5)
check('"Marketing Intern" still excluded', score("Marketing Intern").score == -99)

print("\n#2/#19 malformed row fields")
for bad in [{"ai_salary_min_value":"150,000","ai_salary_max_value":"200,000"},
            {"ai_salary_min_value":"150000.0","ai_salary_max_value":"2e5"},
            {"ai_salary_min_value":"competitive","ai_salary_max_value":"lots"},
            {"locations_derived":{"city":"Austin"}},
            {"locations_derived":[None,None]}]:
    row={"title":"SEO Lead","organization":"X","url":"u",**bad}
    try:
        j=js.IndexedSource._normalize(row); ok=True; d=""
    except Exception as e: ok=False; d=f"{type(e).__name__}: {e}"
    check(f"normalize survives {list(bad)[0]}={list(bad.values())[0]!r}", ok, d)

print("\n#6 null nested location")
j=js.Job(uid="a",title="T",company="C",location=None,url="",ats="t",arrangement="On-site")
try: js.score_job(j,S); check("score_job tolerates location=None", True)
except Exception as e: check("score_job tolerates location=None", False, repr(e))

print("\n#13 Workday URL shapes")
for url,want in [("https://acme.wd1.myworkdayjobs.com/en-US/External","External"),
                 ("https://acme.wd1.myworkdayjobs.com/en-us/External","External"),
                 ("https://www.acme.wd1.myworkdayjobs.com/External","External"),
                 ("https://acme.wd1.myworkdaysite.com/en-US/External","External")]:
    g=js.parse_workday_url(url)          # the real parser, not a copy
    check(f"parses {url[:46]}", bool(g) and g[4]==want, f"got {g}")
check("rejects a non-Workday URL", js.parse_workday_url("https://example.com/x") is None)
import datetime as _dt
_today=_dt.datetime.now(_dt.timezone.utc).date()
check("'Posted Today' -> today's date",
      js._workday_date("Posted Today")==_today.isoformat(), js._workday_date("Posted Today"))
check("'Posted 3 Days Ago' -> exactly three days back",
      js._workday_date("Posted 3 Days Ago")==(_today-_dt.timedelta(days=3)).isoformat(),
      js._workday_date("Posted 3 Days Ago"))
check("'Posted Yesterday' differs from today",
      js._workday_date("Posted Yesterday")!=js._workday_date("Posted Today"))
check("unknown prose -> empty", js._workday_date("whenever")=="")

print("\n#16/#20 store behaviour")
db=tempfile.mktemp(suffix=".sqlite3")
with store.connect(db) as c:
    j=js.Job(uid="u1",title="T",company="C",location="L",url="",ats="t",description="")
    store.upsert(c,j)
    j2=js.Job(uid="u1",title="T",company="C",location="L",url="",ats="t",
              description="full body text", arrangement="Remote OK")
    store.upsert(c,j2)
    row=list(c.execute("SELECT description,arrangement FROM jobs WHERE uid='u1'"))[0]
    check("re-sweep fills a previously empty description", row[0]=="full body text", repr(row[0]))
    check("re-sweep fills arrangement", row[1]=="Remote OK")
    check("set_status reports 0 for unknown uid", store.set_status(c,"NOPE","applied")==0)
    check("set_status reports 1 for real uid", store.set_status(c,"u1","applied")==1)

print("\n#17/#10 config guards")
import copy
# os.devnull and tempdir rather than /dev/null and /tmp, so this block also
# runs under native Windows Python. The environment is passed through — a
# user-site pip install needs APPDATA to be importable on Windows — with the
# API keys removed, so a real token in the shell can never reach the API.
NULL = os.devnull
TMP = tempfile.gettempdir()
ENV = {k: v for k, v in os.environ.items()
       if k not in ("APIFY_TOKEN", "ANTHROPIC_API_KEY")}

def run(cfgdict, args=("--dry-run",), cwd=TMP):
    f=tempfile.NamedTemporaryFile("w",suffix=".yaml",delete=False)
    yaml.dump(cfgdict,f); f.close()
    r=subprocess.run([sys.executable,str(pathlib.Path(__file__).parent/"jobsweep.py"),
                      "--config",f.name,"--local",NULL,*args],
                     capture_output=True,text=True,cwd=cwd,timeout=90,
                     env={**ENV,"APIFY_TOKEN":"dummy"})
    return r
base=copy.deepcopy(cfg); base["output"]["db"]=tempfile.mktemp(suffix=".sqlite3")
c1=copy.deepcopy(base); c1["source"]["indexed"]["queries"]=[]
r=run(c1); check("empty queries refuses to run", r.returncode!=0 and "empty" in (r.stdout+r.stderr).lower(),
                 (r.stdout+r.stderr)[-120:])
c2=copy.deepcopy(base); c2["source"]["mode"]="direct"
r=run(c2); check("mode=direct without a direct block errors clearly",
                 r.returncode!=0 and "no source.direct" in (r.stdout+r.stderr),
                 (r.stdout+r.stderr)[-120:])
c3=copy.deepcopy(base); c3["source"]={"mode":"direct","direct":{"greenhouse":None}}
r=run(c3); check("null slug list doesn't TypeError", "TypeError" not in r.stderr, r.stderr[-120:])
# --explain needs no network, so it is the one command that can be run
# end to end offline against a stored posting.
c4=copy.deepcopy(base)
c4["output"]["db"]=tempfile.mktemp(suffix=".sqlite3")
with store.connect(c4["output"]["db"]) as _c:
    store.upsert(_c, js.Job(uid="led1",title="Senior SEO Manager",company="C",
                            location="Remote",url="https://job-boards.greenhouse.io/a/1",
                            ats="greenhouse",description="organic search"))
_f=tempfile.NamedTemporaryFile("w",suffix=".yaml",delete=False); yaml.dump(c4,_f); _f.close()
r=subprocess.run([sys.executable, str(HERE/"jobsweep.py"), "--config",_f.name,
                  "--local",NULL,"--explain"],
                 capture_output=True,text=True,timeout=120, env=ENV)
check("--explain runs end to end against a stored posting",
      r.returncode==0 and "Auditing term lists" in r.stdout, (r.stdout+r.stderr)[-140:])
print("\n#R1 regressions introduced by the QA pass itself")
for term, title in [("sr ", "sr director of search"), ("sr.", "sr. seo manager"),
                    (" head of ", "head of organic growth")]:
    check(f"term {term!r} with stray whitespace still matches", js._has(term, title))
check('"Sr Director" gets the seniority point',
      "right level" in "; ".join(score("Sr Director of Search", arrangement="Remote OK").reasons))

print("\n#R2 skipped rows stay off the board but remain reachable")
import tempfile as _tf
db2=_tf.mktemp(suffix=".sqlite3")
with store.connect(db2) as c:
    a=js.Job(uid="d1",title="Live Role",company="C",location="A",url="u1",ats="t")
    b=js.Job(uid="d2",title="Shelved Role",company="C",location="B",url="u2",ats="t")
    a.score=9
    store.upsert(c,a); store.upsert(c,b,status="skipped")
    # all_jobs() with no status must hide skipped rows, or a shelved posting
    # sits on the board at score zero next to the live ones.
    check("all_jobs() hides skipped rows by default",
          all(r["status"] != "skipped" for r in store.all_jobs(c)))
    check("all_jobs('skipped') can still reach them",
          len(store.all_jobs(c, "skipped")) == 1)

print("\n#R4 prefix wildcard and whole-word default")
check('"engineer" does NOT match "engineering"', not js._has("engineer","senior engineering manager"))
check('"engineer*" DOES match "engineering"', js._has("engineer*","senior engineering manager"))
check('"market*" matches "marketing"', js._has("market*","marketing manager"))
check('"*" alone matches nothing', not js._has("*","anything at all"))
check('"intern" still does not match "international"', not js._has("intern","director, international seo"))
check('"intern" still matches "intern"', js._has("intern","marketing intern"))

print("\nGreenhouse description decoding")
from unittest.mock import Mock, patch
for content, expected in [
    ("&lt;p&gt;Growth &amp;amp; marketing&lt;/p&gt;", "Growth & marketing"),
    ("<p>Growth &amp; marketing</p>", "Growth & marketing"),
    (None, ""),
]:
    response = Mock()
    response.json.return_value = {"jobs": [{
        "id": 123, "title": "Marketing Manager", "company_name": "Example",
        "absolute_url": "https://example.com/jobs/123", "content": content,
    }]}
    with patch.object(js.requests, "get", return_value=response):
        job = js.DirectSource({})._greenhouse("example")[0]
    check(f"Greenhouse decodes {content!r}",
          job.description.strip() == expected, repr(job.description))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
