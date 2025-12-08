document.getElementById('terminateBtn').addEventListener('click', () => {
  const status = document.getElementById('status');
  status.textContent = '💀 Termination initiated...';
  status.style.color = '#ff4444';

  // Send message to content script
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    chrome.tabs.sendMessage(tabs[0].id, { action: 'terminate' }, (response) => {
      if (response?.status) {
        status.textContent = `✅ ${response.status}`;
        status.style.color = '#00ff88';
      }
    });
  });
});
