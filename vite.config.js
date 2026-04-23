import { defineConfig, loadEnv } from 'vite'
import fs from 'node:fs'
import path from 'node:path'
import react from '@vitejs/plugin-react'
import { ingestApiPlugin } from './vite-plugins/ingestApi.js'

// Minimal .env parser — Vite's loadEnv lets an EMPTY process.env value shadow
// the .env.local value (Claude Code sets ANTHROPIC_API_KEY='' in the shell),
// so we re-read the files ourselves and only keep non-empty values.
function parseEnvFile(p) {
  if (!fs.existsSync(p)) return {}
  const out = {}
  for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/)
    if (!m || line.trim().startsWith('#')) continue
    let v = m[2]
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1)
    }
    out[m[1]] = v
  }
  return out
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // loadEnv reads .env / .env.local etc. from project root.
  // Third arg '' means load every key (not just VITE_*-prefixed).
  const env = loadEnv(mode, process.cwd(), '')
  // Overlay file values on top so empty shell vars don't win.
  const root = process.cwd()
  for (const f of ['.env', '.env.local', `.env.${mode}`, `.env.${mode}.local`]) {
    const parsed = parseEnvFile(path.join(root, f))
    for (const [k, v] of Object.entries(parsed)) if (v !== '') env[k] = v
  }

  return {
    plugins: [
      react(),
      ingestApiPlugin(env),
    ],
    server: { port: 5180, strictPort: true },
  }
})
