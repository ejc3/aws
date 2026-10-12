// Stands in until dolphin-labs-hq/dolphin-maps deploys the loops (deploy-loop.yml).
// Terraform creates the function with this file (index.handler, as the bundle) and then leaves its code alone.
export const handler = async () => {
  throw new Error("dolphin-maps-loop: no code deployed yet");
};
