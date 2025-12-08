let lastMessageIndex;
let isStreaming = false;

// --- SEND MESSAGE ---
function sendMessage(text = "Hello World!") {
  isStreaming = false;
  if (!isStreaming){
    
    const finput = document.querySelector('div[contenteditable="true"] p');

    if (!finput) {
      console.log("Input not found, retrying.a..");
      return setTimeout(() => sendMessage(text), 500);
    }

    console.log("Input found!");  
    ch_traffic('f')

    finput.textContent = text;
    finput.dispatchEvent(new InputEvent("input", { bubbles: true }));

    // Click send button
    const sendBtn = document.querySelector("#composer-submit-button");
    if (!sendBtn) {
      console.log("Send button not found, retrying...");
      return setTimeout(() => sendMessage(text), 300);
    }
    isStreaming = true
    ch_traffic('b')

    lastMessageIndex = document.querySelectorAll('article[data-turn="assistant"]').length;
    console.log("Button found, clicking...");
    sendBtn.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, view: window })
    );
    waitForAIResponse();
  } else {
    console.log("Sorry, now it is streaming")
  }
}

// --- WAIT FOR AI RESPONSE (detect copy button) ---
function waitForAIResponse() {
  console.log(lastMessageIndex)
  const messages = document.querySelectorAll('article[data-turn="assistant"]');
  const lastMessage = messages[lastMessageIndex];

  if (!lastMessage) {
    console.log("No AI message yet, retrying...");
    return setTimeout(waitForAIResponse, 500);
  }

  const copyButton = lastMessage.querySelector('[aria-label="Copy"]');
  console.log(copyButton)

  if (!copyButton) {
    console.log("AI still streaming...");
    return setTimeout(waitForAIResponse, 500);
  }
  scrapeReply()
}

// --- SCRAPE AI MESSAGE ---
function scrapeReply() {
  const messages = document.querySelectorAll('article[data-turn="assistant"]');
  const lastMessage = messages[lastMessageIndex];

  if (!lastMessage) {
    console.log("No message found, retrying...");
    return setTimeout(scrapeReply, 500);
  }
  isStreaming = false;
  // Grab all <p> inside the last message and join them
  const paragraphs = lastMessage.querySelectorAll('p');
  const fullText = Array.from(paragraphs)
                        .map(p => p.innerText.trim())
                        .filter(t => t.length > 0)
                        .join("\n\n"); // separate paragraphs

  if (fullText === "") {
    console.log("No text yet, retrying...");
    return setTimeout(scrapeReply, 500);
  }


  const messageId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const payload = {
    id: messageId,
    text: fullText,
    timestamp: new Date().toISOString(),
    origin: "ai_scraper"
  };

  console.log("AI said:", fullText);
  console.log("🛰️ Dispatching payload to route/background:", payload);
  forwardMessage(payload, 'stt')
  console.log("🚀 AI message handed to forwardMessage:", payload);
  ch_traffic('f')
}


// --- START ---
function scrapAI() {
  sendMessage("Hello chat!");
}
