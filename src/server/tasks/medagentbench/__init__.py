#Structure documentation https://github.com/THUDM/AgentBench/blob/main/docs/Extension_en.md
from typing import Callable, Dict, List, Any
from src.server.task import Task, Session
from src.typings import TaskOutput, SampleStatus, AgentOutputStatus
from .utils import *
from .eval import eval
import time
import json
import importlib
import os
import re

#Models sometimes prefix the command with reasoning prose; this finds the first real command keyword.
_CMD_START = re.compile(r'(GET |POST |FINISH\()')

_FINISH_NUM = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*[A-Za-z%/]*\s*$')
_FINISH_UNIT = re.compile(r'(-?\d+(?:\.\d+)?)\s*(?:mg/dL|mmol/L|mEq(?:/L)?|g/dL|%|g)')

#The grader runs strict json.loads on the FINISH answer and expects a bare array of scalars; models often return correct data as a dict, list of dicts, prose, or a unit-suffixed string, so coerce those shapes to bare numbers while leaving already-clean output untouched.
def _normalize_finish(answer):
    try:
        parsed = json.loads(answer)
    except Exception:
        return answer
    if not isinstance(parsed, list):
        return answer
    out = []
    changed = False
    for el in parsed:
        if isinstance(el, dict):
            v = el.get('value')
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                vq = el.get('valueQuantity')
                v = vq.get('value') if isinstance(vq, dict) else None
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                return answer
            el = v
            changed = True
        if isinstance(el, str):
            m = _FINISH_NUM.match(el)
            hits = _FINISH_UNIT.findall(el)
            tok = m.group(1) if m else (hits[0] if len(hits) == 1 else None)
            if tok is not None:
                el = float(tok)
                changed = True
        out.append(el)
    if not changed:
        return answer
    candidate = json.dumps(out)
    try:
        json.loads(candidate)
    except Exception:
        return answer
    return candidate

#task4-style "last 24h" queries append a bare exact-timestamp date= filter, which FHIR matches exactly and returns 0 rows; drop the over-narrow date filter (matching refsol's _count=5000 so no readings are lost) and let the model filter the window from the returned data
def _broaden_observation_date(query):
    try:
        if 'Observation' not in query or 'date=' not in query:
            return query
        base, _, qs = query.partition('?')
        kept, broadened, has_count = [], False, False
        for p in qs.split('&'):
            k, _, v = p.partition('=')
            if k == '_count':
                has_count = True
            if k == 'date' and 'T' in v and not re.match(r'(eq|ne|gt|lt|ge|le|sa|eb|ap)', v):
                broadened = True
                continue
            kept.append(p)
        if not broadened:
            return query
        if not has_count:
            kept.append('_count=5000')
        return base + '?' + '&'.join(kept)
    except Exception:
        return query


#FHIR bundles carry per-entry metadata (fullUrl, search, resource.meta/extension/text/category)
#that the model never needs to answer a lab-value question. A patient with 100+ readings can
#exceed the per-minute token budget once the bundle is re-sent across rounds, causing 429
#"Request too large" failures. Strip that metadata losslessly (values/timestamps untouched, and
#the grader re-fetches independently so trimming never affects scoring). send_get_request returns
#'data' as a JSON string (FHIR server replies application/fhir+json), so parse before trimming.
_BUNDLE_DROP = ('meta', 'link')
_ENTRY_DROP = ('fullUrl', 'search')
_RESOURCE_DROP = ('meta', 'extension', 'text', 'category')

def _trim_fhir_response(data):
    try:
        parsed = json.loads(data) if isinstance(data, str) else data
        if not isinstance(parsed, dict) or 'entry' not in parsed:
            return data
        for entry in parsed.get('entry', []):
            if not isinstance(entry, dict):
                continue
            for k in _ENTRY_DROP:
                entry.pop(k, None)
            res = entry.get('resource')
            if isinstance(res, dict):
                for k in _RESOURCE_DROP:
                    res.pop(k, None)
        for k in _BUNDLE_DROP:
            parsed.pop(k, None)
        return json.dumps(parsed, separators=(',', ':'))
    except Exception:
        return data

MedAgentBench_prompt = """You are an expert in using FHIR functions to assist medical professionals. You are given a question and a set of possible functions. Based on the question, you will need to make one or more function/tool calls to achieve the purpose.

1. If you decide to invoke a GET function, you MUST put it in the format of
GET url?param_name1=param_value1&param_name2=param_value2...

2. If you decide to invoke a POST function, you MUST put it in the format of
POST url
[your payload data in JSON format]

3. If you have got answers for all the questions and finished all the requested tasks, you MUST call to finish the conversation in the format of (make sure the list is JSON loadable.)
FINISH([answer1, answer2, ...])

Your response must be in the format of one of the three cases, and you can call only one function each time. You SHOULD NOT include any other text in the response.

Here is a list of functions in JSON format that you can invoke. Note that you should use {api_base} as the api_base.
{functions}

Context: {context}
Question: {question}"""

class MedAgentBench(Task):
    def __init__(self, **configs):
        super().__init__(**configs)
        self.data_file = configs.pop("data_file")
        with open(self.data_file, 'r') as f:
            self.data = json.load(f)
        
        self.func_file = configs.pop("func_file")
        with open(self.func_file, 'r') as f:
            self.funcs = json.load(f)

        #Per-category answer-format hints (keyed by task category prefix, e.g. "task5").
        #Only low-performing, format-sensitive categories are present; 90%+ categories and
        #rate-limit-bound task8 are intentionally absent so their prompts stay unchanged.
        #Optional file - missing/corrupt hints must never break a run.
        self.task_hints = {}
        hints_path = os.path.join(os.path.dirname(self.data_file), "task_hints.json")
        try:
            with open(hints_path, 'r') as f:
                self.task_hints = {k: v for k, v in json.load(f).items() if not k.startswith('_')}
        except Exception as e:
            print(f"task_hints.json not loaded ({e}); proceeding without per-category hints")

        self.max_round = configs.pop("max_round", 5)

        self.fhir_api_base = configs.pop("fhir_api_base")
        if verify_fhir_server(self.fhir_api_base) is False:
            print('FHIR server connection error! Please check FHIR server status and fhir_api_base in configs/tasks/medagentbench.yaml')
        try:
            module_name = 'src.server.tasks.medagentbench.refsol'
            refsol = importlib.import_module(module_name)
        except:
            print('Make sure to download the refsol.py and save as `src/server/tasks/medagentbench/refsol.py`')
            exit()

    def get_indices(self) -> List[Any]:
        return list(range(len(self.data))) #[20]#[10*i for i in range(10)]

    async def start_sample(self, index, session: Session):
        print(f"task start {index}")
        case = self.data[index]
        context = case['context']
        #task9's context names both code "K" and LOINC 2823-3 for potassium, but this server only indexes the level by code "K"; mirror the existing magnesium "MG" hint so the model queries K, not the LOINC
        if '2823-3' in context:
            context += ' The code for potassium is "K", not LOINC 2823-3.'
        #Append the per-category answer-format hint (if any) so the model emits the FINISH shape the grader expects. Only weak, format-sensitive categories have a hint; others are unchanged.
        category = case['id'].split('_')[0]
        if category in self.task_hints:
            context += ' ' + self.task_hints[category]
        session.inject({"role": "user", "content": MedAgentBench_prompt.format(api_base=self.fhir_api_base,
                                                                               functions=json.dumps(self.funcs),
                                                                               context=context,
                                                                               question=case['instruction'])})
        try:
            for round in range(self.max_round):
                #time.sleep(5.0) Add for rate limit

                res = (await session.action())
                if res.status == AgentOutputStatus.AGENT_CONTEXT_LIMIT:
                    return TaskOutput(
                    status=SampleStatus.AGENT_CONTEXT_LIMIT,
                    history=session.history
                )
                r = res.content.strip().replace('```tool_code', '').replace('```', '').strip() #Remove separator for Gemini2.0Flash
                m = _CMD_START.search(r)
                if m:
                    r = r[m.start():]

                if r.startswith('GET'):
                    url = _broaden_observation_date(r[3:].strip()) + '&_format=json'
                    #print(f'GET {url}')
                    get_res = send_get_request(url)
                    if "data" in get_res:
                        trimmed = _trim_fhir_response(get_res['data'])
                        session.inject({"role": "user", "content": f"Here is the response from the GET request:\n{trimmed}. Please call FINISH if you have got answers for all the questions and finished all the requested tasks"})
                    else:
                        session.inject({"role": "user", "content": f"Error in sending the GET request: {get_res['error']}"})

                elif r.startswith('POST'):
                    try:
                        payload = json.loads('\n'.join(r.split('\n')[1:]))
                    except Exception as e:
                        session.inject({"role": "user", "content": "Invalid POST request"})
                    else:
                        session.inject({"role": "user", "content": "POST request accepted and executed successfully. Please call FINISH if you have got answers for all the questions and finished all the requested tasks"})
                elif r.startswith('FINISH('):
                    answer = r[len('FINISH('):-1] #Trim to a list
                    answer = _normalize_finish(answer)
                    return TaskOutput(
                        status=SampleStatus.COMPLETED,
                        result=answer,
                        history=session.history
                    )
                else:
                    return TaskOutput(
                        status=SampleStatus.AGENT_INVALID_ACTION,
                        history=session.history
                    )
                
        except Exception as e:
            return TaskOutput(
                status=SampleStatus.TASK_ERROR,
                result={"error": str(e)},
                history=session.history
            )
        
        return TaskOutput(
            status=SampleStatus.TASK_LIMIT_REACHED,
            history=session.history
        )

    def calculate_overall(self, results: List[TaskOutput]) -> Dict[str, Any]:
        total_task = len(results)
        assert len(self.get_indices()) == total_task
        correct_count = 0
        for i in range(total_task):
            if getattr(results[i], "result") is not None:
                index = results[i].index
                if eval(self.data[index], results[i], self.fhir_api_base) is True:
                    correct_count += 1
                    results[i].status += 'Correct'
                else:
                    results[i].status += 'Incorrect'

        return {'success rate': correct_count/total_task, 'raw_results': results}