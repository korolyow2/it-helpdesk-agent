// Keep incremental delivery visible when a provider or application batches text.
export async function renderReply(text, onResponse, {
  overwrite = false,
  animate = true,
  pause = () => new Promise(resolve => setTimeout(resolve, 40)),
} = {}) {
  if (!animate) return onResponse({text, ...(overwrite ? {overwrite: true} : {})});

  const words = text.match(/\S+\s*|\s+/gu) || [];
  for (let index = 0; index < words.length; index++) {
    await onResponse({
      text: words[index],
      ...(overwrite && index === 0 ? {overwrite: true} : {}),
    });
    if (index + 1 < words.length) await pause();
  }
}
