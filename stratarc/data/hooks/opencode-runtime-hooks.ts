const hookRoot = `${process.env.HOME ?? ""}/.config/opencode/hooks`

type Args = Record<string, unknown>

function value(args: Args, ...names: string[]): string {
  for (const name of names) {
    const candidate = args[name]
    if (typeof candidate === "string") return candidate
  }
  return ""
}

// callID is OpenCode's id for one tool call, carried as tool_use_id so a
// guard's deny record names the call it denied and the subagent cap can
// release the reservation it made for it (G-02; callID is in the input of
// both tool hooks, verified 2026-09-24 in the OpenCode 1.4.2 binary).
function payload(event: string, sessionID: string, cwd: string, toolName?: string, toolInput?: Args, callID?: string): string {
  return JSON.stringify({ hook_event_name: event, session_id: sessionID, cwd, tool_name: toolName, tool_input: toolInput, tool_use_id: callID })
}

// The subagent cap's start and finish signals (G-02): a task subagent runs in
// a child session, so the child's session.created confirms the reservation
// its task call made and the child going idle or being deleted finishes it.
// session.created and session.deleted carry info with parentID; session.idle
// carries only sessionID, so each child's parent is remembered from its
// creation (verified 2026-09-24, the event schemas in the OpenCode 1.4.2 binary).
function subagentPayload(event: string, parentID: string, childID: string, cwd: string): string {
  return JSON.stringify({ hook_event_name: event, session_id: parentID, agent_id: childID, cwd })
}

async function invoke(script: string, input: string, cwd: string): Promise<void> {
  const child = Bun.spawn(["bash", `${hookRoot}/${script}`], { cwd, stdin: "pipe", stdout: "pipe", stderr: "inherit" })
  child.stdin.write(input)
  child.stdin.end()
  const stdout = await new Response(child.stdout).text()
  const exitCode = await child.exited
  if (exitCode !== 0) throw new Error(`${script} failed with exit code ${exitCode}`)
  if (!stdout) return
  try {
    const response = JSON.parse(stdout)
    const output = response?.hookSpecificOutput
    if (output?.permissionDecision === "deny") throw new Error(output.permissionDecisionReason || "Hook denied this action")
  } catch (error) {
    if (error instanceof SyntaxError) return
    throw error
  }
}

// For a hook the control plane can switch off per column: skip it when it was
// not deployed, since a missing script exits 127 and invoke() would then block
// every tool call.
async function invokeIfPresent(script: string, input: string, cwd: string): Promise<void> {
  if (!(await Bun.file(`${hookRoot}/${script}`).exists())) return
  await invoke(script, input, cwd)
}

// SessionStart's payload, with source "startup" because session.created fires
// once, when a session is new; session-start.sh acts only on a fresh start.
function sessionStartPayload(sessionID: string, cwd: string): string {
  return JSON.stringify({ hook_event_name: "SessionStart", session_id: sessionID, cwd, source: "startup" })
}

type SessionEvent = { type: string; properties?: { sessionID?: string; info?: { id?: string; parentID?: string } } }

function editedInput(tool: string, args: Args): Args | undefined {
  const filePath = value(args, "filePath", "file_path")
  if (tool === "write") return { file_path: filePath, content: value(args, "content") }
  if (tool === "edit") return { file_path: filePath, old_string: value(args, "oldString", "old_string"), new_string: value(args, "newString", "new_string") }
  if (tool !== "apply_patch") return undefined
  const patch = value(args, "patchText", "patch_text")
  const files = [...patch.matchAll(/^\*\*\* (?:Add|Update) File: (.+)$/gm)].map((match) => match[1])
  const additions = patch.split("\n").filter((line) => line.startsWith("+") && !line.startsWith("+++" )).map((line) => line.slice(1)).join("\n")
  return { file_path: files[0] ?? "", content: additions }
}

export const LlmRootHooks = async ({ directory, worktree }: { directory: string; worktree?: string }) => {
  const cwd = worktree || directory
  // child session id -> parent session id, for every child seen created.
  const children = new Map<string, string>()
  return {
    // OpenCode's session.created event carries SessionStart (WI-32 item 5;
    // the event is in OpenCode 1.4.2's binary and plugin documentation). A
    // task subagent's child session names its parentID and is skipped for
    // SessionStart, so a work item joins once per session rather than once
    // per dispatch; for the subagent cap it is the start signal instead.
    event: async ({ event }: { event: SessionEvent }) => {
      const info = event.properties?.info
      if (event.type === "session.created") {
        if (info?.parentID) {
          children.set(info.id ?? "", info.parentID)
          await invokeIfPresent("subagent-cap-guard.sh", subagentPayload("SubagentStart", info.parentID, info.id ?? "", cwd), cwd)
          return
        }
        await invokeIfPresent("session-start.sh", sessionStartPayload(info?.id ?? "", cwd), cwd)
        return
      }
      if (event.type === "session.idle" || event.type === "session.deleted") {
        const childID = event.properties?.sessionID || info?.id || ""
        const parentID = children.get(childID) || info?.parentID
        if (!parentID) return
        children.delete(childID)
        await invokeIfPresent("subagent-cap-guard.sh", subagentPayload("SubagentStop", parentID, childID, cwd), cwd)
      }
    },
    "tool.execute.before": async (input: { tool: string; sessionID?: string; sessionId?: string; callID?: string }, output: { args: Args }) => {
      const id = input.sessionID || input.sessionId || ""
      const call = input.callID
      // Every tool call counts toward the session's tool-call budget; a spent
      // budget exits 2, which invoke() turns into a block.
      await invokeIfPresent("tool-budget-guard.sh", payload("PreToolUse", id, cwd, input.tool, output.args, call), cwd)
      const edit = editedInput(input.tool, output.args)
      if (edit) {
        await invoke("prose-guard.sh", payload("PreToolUse", id, cwd, "Write", edit, call), cwd)
        await invoke("no-attribution-guard.sh", payload("PreToolUse", id, cwd, "Write", edit, call), cwd)
      }
      if (input.tool === "bash") {
        await invoke("no-attribution-guard.sh", payload("PreToolUse", id, cwd, "Bash", output.args, call), cwd)
        // the no-secret-exposure rule: deny a command that prints an environment.
        await invoke("env-dump-guard.sh", payload("PreToolUse", id, cwd, "Bash", output.args, call), cwd)
      }
      if (input.tool === "task") await invoke("subagent-cap-guard.sh", payload("PreToolUse", id, cwd, "Task", output.args, call), cwd)
    },
    "tool.execute.after": async (input: { tool: string; sessionID?: string; sessionId?: string; callID?: string; args: Args }) => {
      const id = input.sessionID || input.sessionId || ""
      const edit = editedInput(input.tool, input.args)
      if (edit) await invoke("reconcile-control-plane.sh", payload("PostToolUse", id, cwd, "Write", edit, input.callID), cwd)
      if (input.tool === "task") await invoke("subagent-cap-guard.sh", payload("PostToolUse", id, cwd, "Task", {}, input.callID), cwd)
    },
  }
}
