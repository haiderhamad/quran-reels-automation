// Synthetic authorizations used exclusively with invented delivery text.
export default {
  policy_version: "calm-inviting-v1",
  ranges: [
    { surah: 2, first_ayah: 21, last_ayah: 21 },
    { surah: 2, first_ayah: 81, last_ayah: 83 },
  ],
  context_requirements: [{ required_when_contains: "2:82", require_any_of: ["2:81"] }],
};
