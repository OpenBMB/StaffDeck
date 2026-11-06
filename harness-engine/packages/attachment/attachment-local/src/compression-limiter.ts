/** Instance-owned concurrency bound for native image transformations. */

/** FIFO limiter for asynchronous compression work. */
export class CompressionLimiter {
  private active = 0
  private readonly waiting: Array<() => void> = []

  /**
   * @param concurrency - positive maximum number of active tasks.
   */
  constructor(readonly concurrency: number) {}

  /**
   * Run one task after an instance slot becomes available.
   * @param task - compression operation occupying one slot until settlement.
   * @returns the task result.
   */
  run<T>(task: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const start = (): void => {
        this.active += 1
        const release = (): void => {
          this.active -= 1
          this.waiting.shift()?.()
        }
        void Promise.resolve().then(task).then(
          (value) => {
            release()
            resolve(value)
          },
          (error: unknown) => {
            release()
            reject(error instanceof Error
              ? error
              : new Error('Image compression task rejected with a non-Error value.', { cause: error }))
          },
        )
      }
      if (this.active < this.concurrency) start()
      else this.waiting.push(start)
    })
  }
}
