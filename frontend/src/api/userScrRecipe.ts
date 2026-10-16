export const USER_SCR_RECIPE = 'm2-scr2n-user-snapshot-v1'
export const WINDOWS_SCR_RECIPE = 'm2-scr2n-user-windows-spatial-v1'
export function isUserScrRecipe(value: unknown): value is string {
  return value === USER_SCR_RECIPE || value === WINDOWS_SCR_RECIPE
}
