// 把 engine 的 tsconfig.host.json / tsconfig.client.json 改写成 solution 风格：
// 丢掉 include/exclude（其中的 website/**/*.ts 目录未入库，会导致 TS2307），
// 只保留 references，让 tsc -b 单纯地按依赖顺序编译各子工程并产出 lib/。
// 根工程本身是 noEmit，不参与产物生成，去掉 include 不影响任何构建输出。
//
// 第三个参数起是要从 references 里剔除的路径子串，用于排除源码不完整的可选包。
import { readFileSync, writeFileSync } from 'node:fs'

const [, , input, output, ...dropPatterns] = process.argv
if (!input || !output) {
  console.error('usage: make-solution-tsconfig.mjs <in.json> <out.json> [drop-substring...]')
  process.exit(2)
}

// tsconfig 允许注释和尾逗号，JSON.parse 不认，先粗暴清洗。
const raw = readFileSync(input, 'utf8')
  .replace(/^\s*\/\/.*$/gm, '')
  .replace(/,(\s*[}\]])/g, '$1')

const config = JSON.parse(raw)
delete config.include
delete config.exclude
config.files = []

const before = (config.references ?? []).length
if (dropPatterns.length > 0) {
  config.references = (config.references ?? []).filter(
    (ref) => !dropPatterns.some((pattern) => ref.path.includes(pattern)),
  )
}

writeFileSync(output, JSON.stringify(config, null, 2))
console.log(
  `[make-solution-tsconfig] ${input} -> ${output}, references ${before} -> ${config.references.length}`,
)
