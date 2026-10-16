import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { rmSync } from 'node:fs';

const root = fileURLToPath(new URL('.', import.meta.url));
function run(command, args, cwd) {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', env: { ...process.env, NODE_OPTIONS: '' } });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
run('npm', ['ci', '--prefer-offline', '--no-audit', '--no-fund'], root + 'ui');
run('npm', ['run', 'build'], root + 'ui');
rmSync(root + 'build', { recursive: true, force: true });
run(process.env.UV || 'uv', ['build', '--wheel', '--out-dir', 'dist'], root);
