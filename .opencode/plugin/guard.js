/**
 * opencode's entry point into the board's hard limits.
 *
 * The rules live in `.claude/deny.json` and the checking in `lib/guardlib.py`,
 * exposed to every harness as `bin/guard`. This file only adapts opencode's
 * `tool.execute.before` hook to it, exactly as `.claude/hooks/guard.py` adapts
 * Claude Code's `PreToolUse` — so there is one boundary rather than one per
 * agent, and one deny list rather than one per agent that drifts apart.
 *
 * It lives in the board, next to `opencode.json` and `.claude/`, which is what
 * makes a copy of the board carry its own enforcement for every harness it
 * supports rather than depending on how the machine happens to be configured.
 *
 * This is the only JavaScript in the board, and it is deliberately the least
 * that will do: locate `bin/guard`, hand it the tool call, refuse when it says
 * no. Any logic added here would be a second copy of the limits.
 */

import { execFile } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"

// <board>/.opencode/plugin/guard.js -> <board>/bin/guard. Resolved from this
// file rather than from the working directory, because the agent's cwd is the
// project repository as often as it is the board.
const BOARD = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const GUARD = path.join(BOARD, "bin", "guard")

// Long enough that a slow machine is not mistaken for a missing guard, short
// enough that a hung check does not look like a hung agent.
const TIMEOUT_MS = 10_000

/**
 * Ask `bin/guard` about one tool call.
 *
 * The tool input goes in over stdin, not as an argument: a `write` call carries
 * a whole file, which is larger than an argument list is allowed to be, and a
 * check that had to truncate its subject would not be checking the tool call.
 */
function ask(tool, args, cwd) {
  return new Promise((resolve) => {
    const child = execFile(
      GUARD,
      ["check", "--tool", tool, "--input", "-", "--cwd", cwd, "--quiet"],
      { timeout: TIMEOUT_MS, maxBuffer: 1024 * 1024 },
      (error, _stdout, stderr) => {
        if (!error) return resolve(null)
        // A non-zero exit is a verdict and carries its reason on stderr.
        // Anything else — the guard missing, unreadable, killed by the timeout —
        // means there is no boundary, and an unattended agent without one is the
        // situation the guard exists to prevent. Fail closed and say why.
        const reason = String(stderr || "").trim()
        if (typeof error.code === "number" && reason) return resolve(reason)
        resolve(
          `[guard-unavailable] ${GUARD} could not be run (${error.message.trim()}), ` +
            `so the board's hard limits cannot be checked and no tool call can be ` +
            `allowed. Report this in the task with \`bin/task need-human\`.`,
        )
      },
    )
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(args ?? {}))
  })
}

export const guard = async ({ directory, worktree }) => {
  const cwd = directory || worktree || BOARD
  return {
    "tool.execute.before": async (input, output) => {
      const denial = await ask(input.tool, output.args, cwd)
      // Throwing is how a hook refuses: opencode never runs the tool and hands
      // the message back to the model as the tool's result, so the agent reads
      // the rule's own reason rather than a generic failure.
      if (denial) throw new Error(`Denied by the task board's hard limits: ${denial}`)
    },
  }
}
