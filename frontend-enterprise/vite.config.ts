import path from 'path';
import tailwindcss from '@tailwindcss/vite';
import { loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import svgr from 'vite-plugin-svgr';
import { defineConfig } from 'vitest/config';

// Production evidence records the modules actually included by this consumer.
function businessUiGraph(): Plugin {
  const repoRoot = path.resolve(__dirname, '..');
  const roots = ['packages/staffdeck-business-ui/src', 'frontend-enterprise/src/business-ui'].map(root => path.resolve(repoRoot, root));
  const selected = (id: string) => roots.some(root => id.startsWith(`${root}/`));
  const relative = (id: string) => path.relative(repoRoot, id).replaceAll(path.sep, '/');
  return {
    name: 'staffdeck-business-ui-production-graph',
    generateBundle(_options, bundle) {
      this.emitFile({ type: 'asset', fileName: 'business-ui-modules.json', source: `${JSON.stringify({
        schemaVersion: 1,
        modules: [...this.getModuleIds()].filter(selected).map(relative).sort(),
        chunks: Object.values(bundle).filter(item => item.type === 'chunk').map(chunk => ({
          fileName: chunk.fileName,
          modules: Object.keys(chunk.modules).filter(selected).map(relative).sort(),
        })),
      }, null, 2)}\n` });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react(), tailwindcss(), svgr(), businessUiGraph()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
        '@staffdeck/business-ui': path.resolve(__dirname, '../packages/staffdeck-business-ui/src'),
        react: path.resolve(__dirname, './node_modules/react'),
        'react-dom': path.resolve(__dirname, './node_modules/react-dom'),
        'react-router-dom': path.resolve(__dirname, './node_modules/react-router-dom'),
        'lucide-react': path.resolve(__dirname, './node_modules/lucide-react'),
        'radix-ui': path.resolve(__dirname, './node_modules/radix-ui'),
        cytoscape: path.resolve(__dirname, './node_modules/cytoscape'),
        clsx: path.resolve(__dirname, './node_modules/clsx'),
        'tailwind-merge': path.resolve(__dirname, './node_modules/tailwind-merge'),
      },
    },
    base: '/',
    test: {
      setupFiles: ['./src/test/setup.ts'],
      environmentOptions: {
        jsdom: {
          url: 'http://localhost/',
        },
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/km-ui': { target: env.VITE_PROXY_TARGET || 'http://localhost:8000', changeOrigin: true },
        '/api': {
          target: env.VITE_PROXY_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  };
});
