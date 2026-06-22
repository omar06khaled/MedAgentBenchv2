# MedAgentBench — End-to-End Architecture Flowchart

This document traces a single task through the entire MedAgentBench system, from launch to final score.

> **Viewing tip:** The diagrams below are [Mermaid](https://mermaid.live). They render automatically on GitHub, in VS Code (with a Mermaid extension), or by pasting any block into <https://mermaid.live>.

---

## Diagram 1 — System Startup

```mermaid
flowchart TD
    subgraph START["src/start_task.py — Server Launcher"]
        S1["Entry: python -m src.start_task --config configs/start_task.yaml -a"]
        S2["ConfigLoader.load_from (src/configs.py): resolve YAML imports, merge 'default' into siblings, apply 'overwrite'"]
        S3["spawn subprocess: python -m src.server.task_controller --port 5000"]
        S4{"Controller alive? GET /api/list_workers every 0.5s"}
        S5["spawn 20 workers: python -m src.server.task_worker medagentbench-std --port 5001-5020 --controller :5000"]
        S1 --> S2 --> S3 --> S4
        S4 -->|"no - retry up to 10x"| S4
        S4 -->|yes| S5
    end

    subgraph CTRL["TaskController — task_controller.py — port 5000"]
        C1["FastAPI routes: /receive_heartbeat /start_sample /interact /cancel /get_indices /calculate_overall /list_workers"]
        C2["Sessions GC loop every 240s: clean expired sessions, cancel stale samples"]
        C1 --> C2
    end

    subgraph WKRINIT["Each TaskWorker — src/server/task_worker.py"]
        W1["ConfigLoader loads task_assembly.yaml -> imports medagentbench.yaml, merges 'default' into medagentbench-std"]
        W2["InstanceFactory.create (general.py:20): import module, call MedAgentBench(**params)"]
        W3["MedAgentBench.__init__ (__init__.py:32): load test_data_v2.json -> self.data; funcs_v1.json -> self.funcs; max_round=8; fhir_api_base"]
        W4["verify_fhir_server (utils.py): GET /metadata must return 200"]
        W5["importlib import refsol — exit if missing"]
        W6["FastAPI routes: /start_sample /interact /cancel /get_indices /calculate_overall /worker_status"]
        W7["startup register(): heart_beat() every 8s -> POST /receive_heartbeat"]
        W1 --> W2 --> W3 --> W4 --> W5 --> W6 --> W7
    end

    subgraph HB["Controller.receive_heartbeat (task_controller.py:269)"]
        H1{"Task exists in self.tasks?"}
        H2["Create TaskData indices=0..N-1"]
        H3{"Worker address registered?"}
        H4["Add WorkerData id, address, capacity=1, current=0, status=ALIVE"]
        H5["Update worker.last_visit"]
        H6{"Worker was COMA?"}
        H7["_sync_worker_status: reconcile sessions"]
        H8["Done"]
        H1 -->|no| H2 --> H3
        H1 -->|yes| H3
        H3 -->|no| H4 --> H8
        H3 -->|yes| H5 --> H6
        H6 -->|yes| H7 --> H8
        H6 -->|no| H8
    end

    S5 --> WKRINIT
    W7 -->|"POST /api/receive_heartbeat every 8s"| HB
    S3 --> CTRL
```

---

## Diagram 2 — Client Startup & Scheduling

```mermaid
flowchart TD
    subgraph ASGN["src/assigner.py — Client Process"]
        A1["Entry: python -m src.assigner --config configs/assignments/default.yaml"]
        A2["ConfigLoader: default.yaml -> definition.yaml -> task_assembly.yaml + api_agents.yaml + openai-chat.yaml"]
        A3["'overwrite' in definition.yaml: replace MedAgentBench module with src.client.TaskClient for all tasks (client side)"]
        A4["api_agents gpt-4o-mini -> openai-chat.yaml: HTTPAgent, url=api.openai.com, body={model,temperature:0,max_tokens:2048}, prompter=role_content_dict, return_format=choices[0].message.content"]
        A5["AssignmentConfig.parse_obj + post_validate: validate agents/tasks/concurrency, remove unused, dedupe"]
        A6["Assigner.__init__ (assigner.py:42): create output dir, loop assignments"]
        A7["Skip if overall.json exists"]
        A8["TaskClient.get_indices (client/task.py:26): GET /api/get_indices -> [0..N-1]"]
        A9["Scan runs.jsonl: remove completed indices from remaining_tasks"]
        A10["InstanceFactory.create per agent -> HTTPAgent(...)"]
        A1 --> A2 --> A3 --> A4 --> A5 --> A6 --> A7 --> A8 --> A9 --> A10
    end

    subgraph SCHED["Assigner.start + worker_generator (assigner.py:238)"]
        G1["Build flow net: SRC->agent (cap=free_worker.agent=10); agent->task (cap=remaining=N); task->DST (cap=get_concurrency=alive slots)"]
        G2["Solve Max-Flow (utils/max_flow.py) = min(10, N, 20)"]
        G3["Per flow unit: pop index, dec free_worker, yield (agent, task, index)"]
        G4["Sleep random 5-15s, rebuild graph"]
        G5["No remaining AND running_count==0 -> STOP"]
        G1 --> G2 --> G3 --> G4 --> G1
        G2 -->|"flow=0 all busy"| G4
        G3 --> G5
    end

    subgraph THREAD["Per-Sample Thread (assigner.py:385)"]
        T1["threading.Thread; running_count += 1"]
        T2["TaskClient.run_sample(index, agent) (client/task.py:54)"]
        T1 --> T2
    end

    A10 --> SCHED
    G3 --> THREAD
```

---

## Diagram 3 — Per-Sample Execution & Interaction Loop

```mermaid
flowchart TD
    subgraph RS["TaskClient.run_sample (client/task.py:54)"]
        R1["POST /api/start_sample {name, index:42}"]
    end

    subgraph CTRL_SS["Controller.start_sample (task_controller.py:295)"]
        CS1["Acquire tasks_lock; verify task + index"]
        CS2["Pick ALIVE worker max(capacity-current); current += 1"]
        CS3["Create SessionData session_id=7"]
        CS4["Forward: POST worker /api/start_sample {index:42, session_id:7}"]
        CS1 --> CS2 --> CS3 --> CS4
    end

    subgraph WKR_SS["Worker.start_sample (task_worker.py:127)"]
        WS1["session_lock; check not dup; len(session_map) < concurrency(1)"]
        WS2["Create Session(): history=[]; SessionController locks+semaphores(0)"]
        WS3["asyncio task: task_start_sample_wrapper(42, session, 7); store session_map[7]"]
        WS4["await agent_pull() -> BLOCKS"]
        WS1 --> WS2 --> WS3 --> WS4
    end

    subgraph MAB["MedAgentBench.start_sample (__init__.py:57)"]
        M1["case = self.data[42] {id:T3_042, context, instruction, answer}"]
        M2["Build prompt: api_base + json.dumps(funcs 10 FHIR defs) + context + question"]
        M3["session.inject(user, prompt) -> history len 1"]
        M4["await session.action() (task.py:143)"]
        M5["filter_messages (task.py:112): odd len; _calc_segments; threshold 3500; truncate old + NOTICE"]
        M6["env_pull (task.py:34): set env_output.history; release agent_signal; await env_signal BLOCKS"]
        M1 --> M2 --> M3 --> M4 --> M5 --> M6
    end

    subgraph WKR_RESP["First response back"]
        WR1["agent_pull returns TaskOutput(RUNNING, history)"]
        WR2["Worker->Controller {session_id:7, output:{running, history}}"]
        WR3["Controller: status==RUNNING, no finish, return to client"]
        WR1 --> WR2 --> WR3
    end

    subgraph LOOP["TaskClient Loop (client/task.py:73)"]
        L1{"status == RUNNING?"}
        L2["HTTPAgent.inference(history) (http_agent.py:188)"]
        L4["role_content_dict: agent->assistant; {messages:[...]}"]
        L5["body.update(messages)"]
        L6["POST api.openai.com/v1/chat/completions; timeout 120; 3 retries"]
        L7{"status 200?"}
        L8["check_context_limit -> AgentContextLimitException"]
        L9["return_format -> choices[0].message.content (raw string)"]
        L10["AgentOutput(content=raw, status=NORMAL)"]
        L11["POST /interact {session_id:7, agent_response}"]
        L14["Exit loop -> TaskClientOutput(output)"]
        L1 -->|yes| L2 --> L4 --> L5 --> L6 --> L7
        L7 -->|yes| L9 --> L10 --> L11
        L7 -->|no| L8
        L1 -->|no| L14
    end

    subgraph CTRL_INT["Controller.interact (task_controller.py:358)"]
        CI1["Find session 7; forward POST worker /api/interact"]
        CI2["status != RUNNING -> _finish_session(7): del session, worker.current -= 1"]
        CI1 --> CI2
    end

    subgraph WKR_INT["Worker.interact (task_worker.py:159)"]
        WI1["agent_pull(AgentOutput) (task.py:22): store env_input; release env_signal; await agent_signal BLOCKS"]
        WI2["env_pull returns to session.action; append agent ChatHistoryItem; return AgentOutput"]
        WI1 --> WI2
    end

    subgraph PARSE["Parse Action (__init__.py:74)"]
        P0["r = content.strip().replace tool_code/backticks"]
        P1{"r starts with?"}
        P2["GET: r[3:] + '&_format=json'"]
        P3["send_get_request (utils.py:12): LIVE GET real FHIR :8080"]
        P4{"has 'data'?"}
        P5["inject(user, FHIR bundle + FINISH hint)"]
        P6["inject(user, Error ...)"]
        P7["POST: json.loads(lines[1:])"]
        P8{"JSON OK?"}
        P9["inject(user, POST accepted) — NO HTTP, payload discarded"]
        P10["inject(user, Invalid POST)"]
        P11["FINISH: result = r[7:-1] raw JSON string"]
        P12["TaskOutput COMPLETED, result, history"]
        P13["other"]
        P14["TaskOutput AGENT_INVALID_ACTION"]
        P15{"round < 8?"}
        P16["TaskOutput TASK_LIMIT_REACHED"]
        P17{"AGENT_CONTEXT_LIMIT?"}
        P18["TaskOutput AGENT_CONTEXT_LIMIT"]
        P0 --> P1
        P1 -->|GET| P2 --> P3 --> P4
        P4 -->|yes| P5 --> P15
        P4 -->|no| P6 --> P15
        P1 -->|POST| P7 --> P8
        P8 -->|yes| P9 --> P15
        P8 -->|no| P10 --> P15
        P1 -->|FINISH| P11 --> P12
        P1 -->|other| P13 --> P14
        P15 -->|yes| P17
        P15 -->|no| P16
        P17 -->|yes| P18
        P17 -->|no, next round| M5
    end

    subgraph TERM["Termination (task_worker.py:106)"]
        TT1["wrapper: session_map.pop; env_finish (task.py:42): env_output=final; release agent_signal"]
        TT2["agent_pull returns final; Worker->Controller {status, result, history}"]
        TT3["Controller: _finish_session(7): del, worker.current -= 1"]
        TT1 --> TT2 --> TT3
    end

    R1 --> CS1
    CS4 --> WS1
    WS4 --> M1
    M6 --> WR1
    WR3 --> L1
    L11 --> CI1
    CI1 --> WI1
    WI2 --> P0
    P12 --> TT1
    P14 --> TT1
    P16 --> TT1
    P18 --> TT1
    TT3 -->|result to client| L1
```

---

## Diagram 4 — finish_callback & Grading

```mermaid
flowchart TD
    subgraph CB["Assigner.finish_callback (assigner.py:329)"]
        CB1{"error == NOT_AVAILABLE?"}
        CB2["Re-insert index; free slots; running_count -= 1; retry"]
        CB3{"error not None?"}
        CB4["Warn; auto_retry: re-insert index at front"]
        CB5["Write error.jsonl"]
        CB6["Write runs.jsonl {index, output:{status,result,history}, time}"]
        CB7["finished_count += 1; update tqdm"]
        CB8["record_completion (assigner.py:301): set index; append completions"]
        CB9{"all N done?"}
        CB10["Spawn thread: calculate_overall_worker()"]
        CB11["free slots += 1; running_count -= 1"]
        CB1 -->|yes| CB2
        CB1 -->|no| CB3
        CB3 -->|yes| CB4 --> CB5 --> CB11
        CB3 -->|no| CB6 --> CB7 --> CB8 --> CB9
        CB9 -->|yes| CB10 --> CB11
        CB9 -->|no| CB11
    end

    subgraph OVR["TaskClient.calculate_overall (client/task.py:127)"]
        O1["Compute status fractions + avg/max/min history length"]
        O2["POST /api/calculate_overall {name, results}"]
        O3["Controller: find ALIVE worker; forward"]
        O4["Worker (task_worker.py:233): self.task.calculate_overall(results)"]
        O1 --> O2 --> O3 --> O4
    end

    subgraph CALC["MedAgentBench.calculate_overall (__init__.py:116)"]
        C1["assert len(results)==len(indices); correct_count=0"]
        C2{"result.result not None?"}
        C3["Skip = auto-fail (LIMIT/INVALID/CONTEXT/ERROR)"]
        C4["eval(self.data[index], result, fhir_api_base) (eval.py:8)"]
        C5["task_id = id.split('_')[0] e.g. T3"]
        C6["grader = getattr(refsol, 'T3') (refsol.py, not in repo)"]
        C7["grader(case_data, task_output{result=raw string}, fhir_api_base)"]
        C8{"returns True?"}
        C9["correct_count += 1; status += 'Correct'"]
        C10["status += 'Incorrect'"]
        C11["exception -> False -> Incorrect"]
        C12["return {success rate: correct/total, raw_results}"]
        C1 --> C2
        C2 -->|no| C3
        C2 -->|yes| C4 --> C5 --> C6 --> C7 --> C8
        C8 -->|True| C9
        C8 -->|False/None| C10
        C8 -->|exception| C11
        C9 --> C2
        C10 --> C2
        C11 --> C2
        C2 -->|done| C12
    end

    subgraph WRITE["Final Output (assigner.py:309)"]
        WF1["Assemble {total, validation:{status fractions, history lengths}, custom:{success rate, raw_results}}"]
        WF2["Write outputs/MedAgentBenchv1/gpt-4o-mini/medagentbench-std/: runs.jsonl, overall.json, error.jsonl"]
        WF1 --> WF2
    end

    CB10 --> O1
    O4 --> C1
    C12 --> WF1
```

---

## Key Mechanics

- **GET** calls hit the **real FHIR server** live (`utils.send_get_request`); **POST** calls are **silently simulated** — the payload is JSON-parsed for validity but never sent.
- The async handshake between task code and the agent uses two semaphores (`agent_signal`, `env_signal`) as a coroutine rendezvous — no shared mutable state, no polling.
- The `overwrite` key in `definition.yaml` is what lets the same YAML task definition resolve to a `TaskClient` on the client side and a `MedAgentBench` instance on the server side.
- `refsol.py` is the only proprietary file. Each task type (`T1`, `T2`, …) has its own grader function, dispatched by the prefix of `case['id']`.
- The max-flow scheduler enforces agent-concurrency and task-concurrency limits simultaneously.
