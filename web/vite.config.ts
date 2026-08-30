import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The build output is committed and served by the Python server, so the demo
// never needs a Node toolchain. `npm run dev` proxies the API to that same
// server so development and production hit identical endpoints.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Content-hashed names, because the deploy serves /assets/* as `immutable`.
    // With a fixed filename that header is a lie: a returning visitor keeps the
    // bundle they first downloaded for a year, and never sees a new build.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
})
