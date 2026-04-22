import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { ingestApiPlugin } from './vite-plugins/ingestApi.js'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // loadEnv reads .env / .env.local etc. from project root.
  // Third arg '' means load every key (not just VITE_*-prefixed).
  const env = loadEnv(mode, process.cwd(), '')

  return {
    plugins: [
      react(),
      ingestApiPlugin(env),
    ],
    server: { port: 5180, strictPort: true },
  }
})
