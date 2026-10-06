user default off
user admin on >${SHARED_REDIS_PASSWORD} ~* &* +@all
user hosp_app on >${HOSP_REDIS_PW} ~idem:hosp:* ~cache:hosp:* ~rl:hosp:* ~lock:hosp:* ~sse:hospital:* ~docpipe:* ~vision:* &sse:hospital:* &cfg:changed &audit:appended +@all -@dangerous -@admin +ping +info
user ins_app on >${INS_REDIS_PW} ~idem:ins:* ~rl:ins:* ~lock:ins:* ~sse:insurer:* &sse:insurer:* &cfg:changed &audit:appended +@all -@dangerous -@admin +ping +info
user llm on >${LLM_REDIS_PW} ~llm:* +@all -@dangerous -@admin
user n8n_hosp on >${N8N_HOSP_REDIS_PW} ~bull:* ~n8n:* +@all -@dangerous -@admin
user n8n_ins on >${N8N_INS_REDIS_PW} ~bull:* ~n8n:* +@all -@dangerous -@admin
user docpipe on >${DOCPIPE_REDIS_PW} ~docpipe:* ~lock:docpipe:* +@all -@dangerous -@admin
