console.log("content.js loaded successfully");

let current_page = page();   // e.g. "ai", "class", "teacher", "stt"
let traffic = "busy";

chrome.runtime.sendMessage({
    type: "register_tab",
    role: current_page
});



async function updateStatus(tab, state) {
    try {
        await fetch("http://127.0.0.1:5000/update_status", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                tab: tab,      // string
                status: state  // "busy" or "free"
            })
        });
    } catch (e) {
        console.error("Failed to update status:", e);
    }
}

function ch_traffic(state) {
    if (state === "b" || state === "busy") {
        traffic = "busy";
    } else if (state === "f") {
        traffic = "free";
    } else {
        console.warn("Unknown traffic state:", state);
        return;
    }
    if (current_page != "login" && current_page != "home") {
      updateStatus(current_page, traffic);
    } else{
      return
    }

}
ch_traffic(traffic)

// --- Listen for stat updates from background ---
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.from === "background" && msg.type === "statUpdate" && msg.stat) {
        stat = msg.stat;  // overwrite local stat with the latest
        console.log("🔥 Stat updated in content.js:", stat);
    }
});


async function forwardMessage(message, recipient) {
  const payload =
    typeof message === "string"
      ? {
          id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
          text: message,
          timestamp: new Date().toISOString(),
          origin: `${current_page}_content`
        }
      : {
          ...message,
          id:
            message.id ||
            `${Date.now()}-${Math.random().toString(16).slice(2)}`,
          timestamp: message.timestamp || new Date().toISOString(),
          origin: message.origin || `${current_page}_content`
        };

  console.log("📤 forwardMessage() dispatching payload:", {
    recipient,
    payload
  });

  chrome.runtime.sendMessage({
    type: "relay_message",
    from: current_page,
    to: recipient,
    message: payload
  });

  try {
    const response = await fetch("http://127.0.0.1:5000/send_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from: current_page,       // e.g., "AI", "teacher", "class", "stt"
        to: recipient,      // must match keys in tab_registry: "ai", "teacher", etc.
        message: payload    // the actual text to send
      })
    });

    if (!response.ok) {
      const error = await response.json();
      console.error("Failed to send message:", error);
    } else {
      const data = await response.json();
      console.log("Message sent successfully:", data);
    }
  } catch (err) {
    console.error("Error sending message:", err);
  }
}






function mainf(){
  if (current_page === "login") {
    login();
  } else if (current_page === "home") {
    goStandby();
  } else if (current_page === "class") {
    startTeaching();
  } else if (current_page === "ai") {
    scrapAI();
  } else if (current_page === "teacher"){
    feedModel()
  } else if (current_page === "stt"){
    console.log("stt")
  };
}
mainf()



chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'terminate') {
    console.log("🚨 Terminate command received from popup!");

    sendResponse({ status: 'System terminated!' });
  }
});
