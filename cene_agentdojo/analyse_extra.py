"""Extra analyses for both models: per-injection-goal results, paired McNemar tests (M2 vs M4/M6/M8) and overhead.

    python -m cene_agentdojo.analyse_extra GEMMA_RAW_DIR QWEN_RAW_DIR OUT.json

Added verbatim from outputs/Kaggle/model2-RUN-3/extra_analyses.py (only this docstring is new). Analysis only.
per_injection[inj][Mx] = [attack successes, hijacked, errored] over non-errored pairs (errored pairs counted
in the third field only). paired: errored pairs left out; p_exact is the two-sided exact McNemar p (1.0 when
there are no discordant pairs). overhead: means over non-errored episodes.
"""
import json, sys
from math import comb
from pathlib import Path
DIRS={'gemma':Path(sys.argv[1]),'qwen':Path(sys.argv[2])}
def load(m,c): return json.load(open(DIRS[m]/f'agentdojo_{c}.json'))['episodes']
def mcnemar(b,c):
    n=b+c
    if n==0: return 1.0
    k=min(b,c); p=2*sum(comb(n,i) for i in range(k+1))/2**n
    return min(1.0,p)
out={}
for m in DIRS:
    E={c:load(m,c) for c in ['M1','M2','M3','M4','M5','M6','M7','M8']}
    o=out[m]={}
    # per injection
    inj={}
    for c in ['M2','M4','M6','M8']:
        for e in E[c].values():
            r=inj.setdefault(e['injection_task'],{}).setdefault(c,[0,0,0])
            if e['error']: r[2]+=1; continue
            r[0]+=bool(e['security']); r[1]+=bool(e.get('hijacked'))
    o['per_injection']=inj
    # paired
    pr={}
    for c in ['M4','M6','M8']:
        for metric in ['security','utility']:
            b=cc=n=0
            for k,e2 in E['M2'].items():
                ex=E[c][k.replace('M2',c,1)]
                if e2['error'] or ex['error']: continue
                n+=1; a1,a2=bool(e2[metric]),bool(ex[metric])
                if a1 and not a2: b+=1
                if a2 and not a1: cc+=1
            pr[f'M2_vs_{c}_{metric}']={'n':n,'M2_only':b,f'{c}_only':cc,'p_exact':mcnemar(b,cc)}
    o['paired']=pr
    # overhead
    ov={}
    for c,eps in E.items():
        ok=[e for e in eps.values() if not e['error']]
        ls=[s for e in ok for s in (e.get('llm_call_seconds') or [])]
        ov[c]={'episodes':len(ok),'model_calls':round(sum(e['model_calls'] for e in ok)/len(ok),2),
               'tool_calls':round(sum(e['tool_calls'] for e in ok)/len(ok),2),
               'seconds':round(sum(e['seconds'] for e in ok)/len(ok),1),
               'llm_seconds_per_episode':round(sum(ls)/len(ok),1),
               'non_llm_seconds_per_episode':round((sum(e['seconds'] for e in ok)-sum(ls))/len(ok),2)}
    o['overhead']=ov
json.dump(out,open(sys.argv[3],'w'),indent=1)
for m in out:
    print('##',m)
    for i,v in sorted(out[m]['per_injection'].items()): print(' ',i,v)
    for k,v in out[m]['paired'].items(): print(' ',k,v)
    for k,v in out[m]['overhead'].items(): print(' ',k,v)
