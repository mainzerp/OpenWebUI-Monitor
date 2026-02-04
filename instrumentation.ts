// This file runs once when the Next.js server starts
export async function register() {
    if (process.env.NEXT_RUNTIME === 'nodejs') {
        const { ensureRuntimeInitialized } = await import('@/lib/runtime')
        await ensureRuntimeInitialized()
    }
}
