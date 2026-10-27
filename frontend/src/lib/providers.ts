// One display name per provider kind (audit C-32). The offline mock provider always reads
// "Offline demo (mock)", whatever label an older server's catalog sends.

export const MOCK_PROVIDER = "mock";
export const MOCK_PROVIDER_LABEL = "Offline demo (mock)";

export function providerKindLabel(key: string, kind?: { label: string }): string {
  if (key === MOCK_PROVIDER) return MOCK_PROVIDER_LABEL;
  return kind?.label ?? key;
}
