// Stands in until dolphin-labs-hq/dolphin-labs deploys the news bundle (deploy-news.yml).
// Terraform creates both functions with this file and then leaves their code alone:
// dolphin-labs-news-pass runs `handler`, dolphin-labs-news-x runs `xHandler`.
export const handler = async () => {
  throw new Error("dolphin-labs-news-pass: no code deployed yet");
};

export const xHandler = async () => {
  throw new Error("dolphin-labs-news-x: no code deployed yet");
};
