// Stands in until dolphin-labs-hq/dolphin-labs deploys the news pass (deploy-news.yml).
// Terraform creates the function with this file and then leaves its code alone.
export const handler = async () => {
  throw new Error("dolphin-labs-news-pass: no code deployed yet");
};
