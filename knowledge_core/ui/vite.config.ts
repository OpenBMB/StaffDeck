import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig(({ mode }) => ({
  base: './',
  plugins: [react(), {
    name: 'knowledge-ui-version',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'version.json', source: JSON.stringify({
        protocol: 'knowledge-frame/v1', version: process.env.KM_UI_VERSION || new Date().toISOString(),
      }) });
    },
  }],
  build: mode === 'library' ? {
    outDir: 'dist', emptyOutDir: true,
    lib: { entry: fileURLToPath(new URL('./src/index.ts', import.meta.url)), formats: ['es'], fileName: 'knowledge-ui', cssFileName: 'knowledge-ui' },
    rollupOptions: { external: ['react', 'react/jsx-runtime', 'react-dom', 'lucide-react', 'marked', 'dompurify'] },
  } : { outDir: fileURLToPath(new URL('../src/knowledge_core/ui', import.meta.url)), emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:18787' } },
}));
