# Method

The Maker is Aider, an autonomous coding agent, operating as an E-DI agent
seat. It receives the packet (round, judge defects, workable solutions), works
inside a confined workspace containing only the artifact files, and may call
its model only through edi-run's metering proxy. It edits `median.py` in the
artifact directory until the task statement is satisfied. The task statement
is fixed in `job.json` under `maker.command` and is frozen with the job.
