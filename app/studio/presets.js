// presets.js - one-click examples that fill the form. Each one shows a hard case for the engine.
// input: "script" | "scenes"; the other fields are the same options the API takes.

export const PRESETS = [
  {
    id: "saving-money",
    label: "Saving money",
    input: "script",
    script:
      "Saving money is not about earning more. It is about spending wisely. In 2025, household savings grew " +
      "by 40%. Cloud computing now lets small businesses work from anywhere. Trust is earned slowly. " +
      "One honest decision at a time.",
    ratios: ["16:9", "9:16"],
    style: "warm, calm, natural light",
    avoid: [],
  },
  {
    id: "double-meanings",
    label: "Double meanings",
    title: "Double meanings (bank, cloud, mouse, python, value)",
    input: "script",
    script:
      "We sat on the river bank and watched the water drift by. Later, I moved my savings to a bank with " +
      "lower fees. Our photos now live in the cloud, not on a hard drive. The cat chased a mouse under the " +
      "sofa, while I clicked the mouse to save my work. My python script sorted the data in seconds. " +
      "Good friends add real value to life.",
    ratios: ["16:9"],
    style: "",
    avoid: [],
  },
  {
    id: "statistics",
    label: "Statistics",
    input: "script",
    script:
      "Almost 40% of small businesses do not survive their first year. Only 1 in 5 people save more than " +
      "10% of their income. Since 2010, online sales have grown by 300%. Small habits change these numbers.",
    ratios: ["16:9", "1:1"],
    style: "clean, modern",
    avoid: [],
  },
  {
    id: "sad-news",
    label: "Sad news (respectful)",
    input: "script",
    script:
      "Today we remember the families who lost their homes in the floods. Volunteers worked through the " +
      "night to bring food, blankets and comfort. Recovery will take time, but the community is standing " +
      "together.",
    ratios: ["16:9"],
    style: "calm, respectful, soft light",
    avoid: ["graphic injuries", "crying faces"],
  },
  {
    id: "named-brand",
    label: "Named brand",
    input: "script",
    script:
      "Apple just announced a new iPhone, and fans lined up outside the stores. Meanwhile, Tesla says its " +
      "new factory will open next year. Big brands move fast, but customers decide what lasts.",
    ratios: ["16:9", "9:16"],
    style: "",
    avoid: ["logos"],
  },
  {
    id: "scene-hints",
    label: "Scene list with hints",
    input: "scenes",
    scenes: [
      { text: "Every morning starts with a cup of coffee.", visual_hint: "steaming coffee cup on a wooden table" },
      { text: "Then the city slowly wakes up.", visual_hint: "aerial city at sunrise with traffic" },
      { text: "By noon, the office is buzzing with ideas.", visual_hint: "bright office with laptops on desks" },
      { text: "And at night, we finally slow down.", visual_hint: "calm lake at dusk" },
    ],
    ratios: ["16:9", "9:16"],
    style: "",
    avoid: [],
  },
];
