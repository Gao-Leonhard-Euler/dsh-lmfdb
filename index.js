/**
 * dsh-lmfdb-tools — the nine LMFDB tools for DeepSeek Harness, natively.
 *
 * This is the package entry both ways the plugin is installed:
 *   - from a git URL / tarball, the package root *is* the bundle, so this file
 *     is what the Harness loads;
 *   - from a local checkout, the bundle lives in dsh/lmfdb-tools/, whose entry
 *     only re-exports this file (see dsh/lmfdb-tools/index.js).
 * Keeping one implementation means the two install paths cannot drift.
 *
 * No MCP hop: the plugin talks to a long-lived Python worker
 * (`agent_tools.py --serve`) which reuses `server.py` unchanged, so the SQL
 * guard, statement timeout, row caps and curation metadata stay in one place.
 *
 * No exposed tool requires the model to write SQL: `lmfdb_find` returns rows
 * from structured filters, and `lmfdb_run_sql` is retired — its manifest entry
 * is commented out in agent_tools.py while the implementation stays in
 * server.py. A custom SELECT can still be handed to `lmfdb_export_query`.
 *
 * The worker is spawned on first use and restarted on crash or timeout, so a
 * dead database mirror degrades to a per-call error instead of a broken tool.
 */
import { execFileSync, spawn } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineTool } from '@deepseek-ai/dsh-tools';

/** Cordis plugin name used by loader diagnostics. */
export const name = 'dsh-lmfdb-tools';

/** Services required before the plugin starts. */
export const inject = ['tools', 'systemPrompt'];

const DEFAULT_TIMEOUT_MS = 300_000;
const WORKER_RESTART_DELAY_MS = 250;

/**
 * The data directory is the package root: agent_tools.py and server.py sit next
 * to this file in both install layouts (git-URL package root, and the local
 * checkout's root that dsh/lmfdb-tools re-exports from).
 */
function defaultRoot(moduleUrl) {
    return path.dirname(fileURLToPath(moduleUrl));
}

/** The model-facing guidance registered with the system prompt. */
const GUIDANCE = [
    'Use lmfdb_* tools for the LMFDB (L-functions and Modular Forms Database) read-only mirror:',
    'lmfdb_overview → curated section map with hub tables (call it when you do not know the table);',
    'lmfdb_list_tables / lmfdb_describe_table → exact table and column names before filtering (column names differ from the mathematics, e.g. ec_curvedata uses lmfdb_label);',
    'lmfdb_search_knowls → find which column means something when you only know the concept;',
    'lmfdb_sample_rows → see real value formats;',
    'lmfdb_count_rows / lmfdb_table_stats → take a structured `where` JSON filter (e.g. {"rank": 2}, {"conductor": {"max": 1000}}, {"st_group": {"in": ["USp(4)"]}}) — no SQL needed;',
    'lmfdb_find → rows from one table: table + where + columns + limit, returns columns/rows directly (the SQL-free lookup);',
    'lmfdb_export_query → table + where + columns, writes a local CSV/JSONL file (use it for bulk instead of pulling rows through the conversation);',
    'Raw SQL is not exposed: for joins or aggregation pass a full SELECT to the optional `sql` argument of lmfdb_export_query.',
    'All tools are read-only. If a tool returns {"error": ...}, read it before retrying: it usually names the offending table or column.',
].join(' ');

/** Resolve config with defaults even when the loader hands over a raw object. */
function resolveConfig(config, moduleUrl) {
    const c = config ?? {};
    const root = typeof c.root === 'string' && c.root.length > 0 ? c.root : defaultRoot(moduleUrl);
    // A GitHub install lives under the profile's node_modules, where writing is
    // unhelpful, so the default export destination is the user's home directory.
    const exportDir = typeof c.exportDir === 'string' && c.exportDir.length > 0
        ? c.exportDir
        : path.join(os.homedir(), 'lmfdb-exports');
    return {
        python: typeof c.python === 'string' && c.python.length > 0 ? c.python : 'python',
        root,
        exportDir,
        // primary backend: "mcp" (default, falls back to the mirror) or "postgres"
        primary: typeof c.primary === 'string' && c.primary.length > 0 ? c.primary : '',
        mcpUrl: typeof c.mcpUrl === 'string' && c.mcpUrl.length > 0 ? c.mcpUrl : '',
        mcpTimeoutMs: Number.isFinite(c.mcpTimeoutMs) ? c.mcpTimeoutMs : 0,
        timeoutMs: Number.isFinite(c.timeoutMs) ? c.timeoutMs : DEFAULT_TIMEOUT_MS,
        script: path.join(root, 'agent_tools.py'),
    };
}

/** Read the tool table from the Python side (single source of truth). */
function loadManifest(cfg) {
    const out = execFileSync(cfg.python, [cfg.script, '--dump-tools'], {
        cwd: cfg.root,
        encoding: 'utf8',
        timeout: 120_000,
        windowsHide: true,
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    });
    const doc = JSON.parse(out);
    if (!Array.isArray(doc.tools) || doc.tools.length === 0)
        throw new Error('empty tool manifest');
    return doc.tools;
}

/** JSON-Schema property -> dsh tool parameter (required/enum/description kept). */
function toParameters(schema) {
    const required = new Set(schema.required ?? []);
    const parameters = {};
    for (const [key, prop] of Object.entries(schema.properties ?? {})) {
        const parameter = {
            type: prop.type === 'number' ? 'number' : prop.type === 'boolean' ? 'boolean' : 'string',
        };
        if (required.has(key))
            parameter.required = true;
        if (prop.description)
            parameter.description = prop.description;
        if (Array.isArray(prop.enum) && prop.enum.length > 0)
            parameter.enum = [...prop.enum];
        parameters[key] = parameter;
    }
    return parameters;
}

/**
 * A long-lived `agent_tools.py --serve` process speaking newline-delimited
 * JSON. Restarted lazily after a crash, a timeout or a closed pipe.
 */
function createRunner(cfg) {
    let child = null;
    let nextId = 1;
    let buffer = '';
    let stderrTail = '';
    const pending = new Map();

    const rejectAll = (reason) => {
        for (const [, entry] of pending) {
            clearTimeout(entry.timer);
            entry.reject(new Error(reason));
        }
        pending.clear();
    };

    const stop = (reason) => {
        const dead = child;
        child = null;
        if (dead !== null) {
            try {
                dead.kill();
            }
            catch {
                /* already gone */
            }
        }
        rejectAll(reason);
    };

    const start = () => {
        const env = {
            ...process.env,
            PYTHONUTF8: '1',
            PYTHONIOENCODING: 'utf-8',
        };
        if (cfg.exportDir.length > 0)
            env.LMFDB_EXPORT_DIR = cfg.exportDir;
        if (cfg.primary.length > 0)
            env.LMFDB_PRIMARY = cfg.primary;
        if (cfg.mcpUrl.length > 0)
            env.LMFDB_MCP_URL = cfg.mcpUrl;
        if (cfg.mcpTimeoutMs > 0)
            env.LMFDB_MCP_TIMEOUT_MS = String(cfg.mcpTimeoutMs);

        const proc = spawn(cfg.python, [cfg.script, '--serve'], {
            cwd: cfg.root,
            env,
            windowsHide: true,
            stdio: ['pipe', 'pipe', 'pipe'],
        });
        child = proc;
        buffer = '';
        stderrTail = '';

        proc.stdout.setEncoding('utf8');
        proc.stdout.on('data', (chunk) => {
            buffer += chunk;
            let newline;
            while ((newline = buffer.indexOf('\n')) >= 0) {
                const line = buffer.slice(0, newline).trim();
                buffer = buffer.slice(newline + 1);
                if (line.length === 0)
                    continue;
                let message;
                try {
                    message = JSON.parse(line);
                }
                catch {
                    continue; // a stray stdout line (e.g. a warning) is not fatal
                }
                const entry = pending.get(message.id);
                if (entry === undefined)
                    continue;
                pending.delete(message.id);
                clearTimeout(entry.timer);
                entry.resolve(message);
            }
        });

        proc.stderr.setEncoding('utf8');
        proc.stderr.on('data', (chunk) => {
            stderrTail = (stderrTail + chunk).slice(-4000);
        });

        proc.on('error', (error) => {
            if (child === proc)
                child = null;
            rejectAll(`lmfdb worker failed to start (${cfg.python}): ${error.message}`);
        });

        proc.on('exit', (code) => {
            if (child === proc)
                child = null;
            const detail = stderrTail.trim().length > 0 ? `\n${stderrTail.trim()}` : '';
            rejectAll(`lmfdb worker exited (code ${code ?? 'null'})${detail}`);
        });
    };

    return {
        call(tool, args) {
            if (child === null) {
                start();
                // Give an immediate spawn failure (missing interpreter) a chance
                // to reject before we write into a half-initialised pipe.
                if (child === null)
                    return Promise.reject(new Error(`cannot start lmfdb worker via ${cfg.python}`));
            }
            const proc = child;
            const id = nextId++;
            return new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                    stop(`lmfdb tool ${tool} timed out after ${cfg.timeoutMs} ms`);
                }, cfg.timeoutMs);
                pending.set(id, { resolve, reject, timer });
                try {
                    proc.stdin.write(`${JSON.stringify({ id, tool, args })}\n`);
                }
                catch (error) {
                    pending.delete(id);
                    clearTimeout(timer);
                    reject(error);
                }
            }).then((response) => {
                if (response.exitCode !== 0) {
                    const detail = String(response.stderr ?? '').trim();
                    throw new Error(detail.length > 0 ? detail : `lmfdb tool ${tool} failed`);
                }
                return String(response.stdout ?? '');
            });
        },
        close() {
            stop('lmfdb plugin disposed');
        },
    };
}

/** Build one dsh tool for one manifest entry. */
function makeTool(tool, cfg, runner) {
    return defineTool({
        name: tool.name,
        description: tool.description,
        parameters: toParameters(tool.inputSchema ?? {}),
        output: {
            schema: { type: 'string' },
            render: (_args, value) => [{ type: 'text', text: String(value) }],
        },
        timeoutMs: cfg.timeoutMs,
        isConcurrencySafe: () => true,
        async execute(args) {
            try {
                return await runner.call(tool.name, args ?? {});
            }
            catch (error) {
                return JSON.stringify({ error: String(error?.message ?? error) });
            }
        },
        presentCall: (args) => ({
            card: 'generic',
            title: tool.name.replace(/^lmfdb_/, ''),
            kind: 'read',
            rawInput: JSON.stringify(args ?? {}),
        }),
        presentResult: () => undefined,
    });
}

/**
 * Register the nine tools. A broken manifest disables the plugin with a log
 * line instead of taking the whole profile down at boot.
 */
export function apply(ctx, config) {
    const cfg = resolveConfig(config, import.meta.url);
    let tools;
    try {
        tools = loadManifest(cfg);
    }
    catch (error) {
        console.error(`[dsh-lmfdb-tools] disabled: cannot read tool manifest (${cfg.script}): ${String(error?.message ?? error)}`);
        return;
    }

    const runner = createRunner(cfg);
    ctx.systemPrompt.section({ name: 'tool:lmfdb', order: 114, text: GUIDANCE });
    for (const tool of tools)
        ctx.tools.register(makeTool(tool, cfg, runner));

    try {
        ctx.on('dispose', () => runner.close());
    }
    catch {
        /* older loaders have no dispose event; the child dies with the app */
    }
}
