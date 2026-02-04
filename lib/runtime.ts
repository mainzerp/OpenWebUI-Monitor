import { initScheduler } from '@/lib/scheduler'
import { ensureUserTableExists } from '@/lib/db/users'

let runtimeInitialized = false

export async function ensureRuntimeInitialized(): Promise<void> {
    if (runtimeInitialized) return
    runtimeInitialized = true

    try {
        // Ensure database schema is ready before starting scheduler
        await ensureUserTableExists()
        initScheduler()
    } catch (error) {
        console.error('[Runtime] Failed to initialize:', error)
    }
}
