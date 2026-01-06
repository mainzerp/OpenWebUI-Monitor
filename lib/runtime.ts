import { initScheduler } from "@/lib/scheduler";

let runtimeInitialized = false;

export function ensureRuntimeInitialized(): void {
  if (runtimeInitialized) return;
  runtimeInitialized = true;

  try {
    initScheduler();
  } catch (error) {
    console.error("[Runtime] Failed to initialize scheduler:", error);
  }
}
