async function checkAllBoxes() {
    let checkboxes = document.querySelectorAll(".request_detail input[type='checkbox']");
    if (checkboxes.length === 0) {
      console.log("No checkboxes found!");
      return;
    }

    for (let cb of checkboxes) {
      if (!cb.checked) {
        await sleep(500);
        cb.click();
        console.log("Checked one box ✅");
        await sleep(200); // wait 0.5 seconds between clicks
      }
    }

    console.log("All boxes checked ✅");

    await sleep(500)
    let modal = document.querySelector("#dialog_lesson_length_cfm");

    if (modal) {
      // Traverse down to the OK button
      let ok_btn = modal.querySelector(".btn_wrap a.close_modal.btn_orange");
      if (ok_btn) {
        ok_btn.click(); // safely click the button
        console.log("Modal OK button clicked ✅");
      } else {
        console.log("OK button not found inside modal!");
      }
    } else {
      console.log("Modal not found!");
    }
  }

let iframeDocument;
function waitForIframeContent(selector = ".tb-cat-list") {
    const iframe = document.querySelector("#textbook-iframe");
    if (!iframe) {
        // Retry if iframe not yet in DOM
        setTimeout(() => waitForIframeContent(selector), 500);
        return;
    } else{
      console.log("Iframe found")
    }
    function checkIframeDoc() {
        const doc = iframe.contentDocument || iframe.contentWindow?.document;
        if (!doc) {
            setTimeout(checkIframeDoc, 500);
            return;
        }
        const target = doc.querySelector(selector);
        if (target) {
            iframeDocument = doc;
            // ✅ First log everything we need internally
            console.log("✅ Iframe content fully ready ✅");

            // ✅ Then fire the custom event
            document.dispatchEvent(new Event("iframeloaded"));
        } else {
            // Retry until element appears
            setTimeout(checkIframeDoc, 500);
        }
    }
    // If iframe already fired load, start checking immediately
    if (iframe.contentDocument && iframe.contentDocument.readyState === "complete") {
        checkIframeDoc();
    } else {
        iframe.addEventListener("load", checkIframeDoc);
    }
}

let bookType;
function textbookType(){
  let iframe = document.querySelector('#textbook-iframe');
  const iframe_body = iframeDocument.body;
  const article = iframeDocument.querySelector("article")
  const test = iframeDocument.querySelector(".student_hide")
  const htmlDirectory = iframe.getAttribute("html-directory")

  bookType = htmlDirectory
  console.log("🔥 Textbook type: " + bookType)
  //Execute the corresponding function to operate the Textbook
  window[bookType]();

}


/// content.js
// Simplified fetch helper
async function fetchFile(docName) {
  const url = chrome.runtime.getURL(`web_accessible_resources/${docName}`);
  
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    return text; // just return the string, no verbose debug
  } catch (err) {
    console.error(`Failed to fetch ${docName}:`, err);
    return null;
  }
}

// Fetch & log wrapper
async function fetchAndLogDoc(docName) {
  const content = await fetchFile(docName);
  console.log(`📄 ${docName}:`, content ? `[length: ${content.length}]` : 'null');
  return content;
}



// OPEARTING BOOKS
let textbookContent = "";

async function daily_news() {
  console.log("Started Terminating Daily News");

  const article = iframeDocument.querySelector("article");
  if (article) {
    textbookContent = article.innerText
      .replace(/[ \t]+/g, " ")        // collapse multiple spaces/tabs
      .split("\n")                    // split lines
      .map(line => line.trim())       // trim each line
      .filter(line => line.length > 0) // drop empty lines
      .join("\n");                    // rebuild clean text
    console.log("🔥 Textbook Content:\n", textbookContent);
  } else {
    console.warn("⚠️ No <article> element found in iframe.");
  }

  // Fetch files sequentially
  const mainPrompt = await fetchAndLogDoc('mainPromt.txt') || "";
  const dailyNews = await fetchAndLogDoc('dailynews.txt') || "";

  // Build message
  const msgBuild = `${mainPrompt}\n\n
  The demo lesson for this type of textbook (${bookType}):\n\n
  ${dailyNews}\n\n
  This is the end of demo lesson. After you learned the flow and drill, you
  must learn the actual textbook content of today:\n
  ${textbookContent}\n
  Now you are ready to greet the student! The student is listening, say hello.
  `;

  console.log("📬 Final Message:\n", msgBuild);
}



function startTeaching(){
  checkAllBoxes();
  waitForIframeContent();
  document.addEventListener("iframeloaded", () => {
    console.log("Iframe is ready globally!");
    textbookType()
  });
}