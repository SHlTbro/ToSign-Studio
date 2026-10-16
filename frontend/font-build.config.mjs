import base from './vite.config.ts'
import { fileURLToPath, URL } from 'node:url'
export default { ...base, cacheDir: fileURLToPath(new URL('../../private-build-cache', import.meta.url)) }
