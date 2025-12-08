function update_status(){
    print("Update Status...")
}


function login(){
  let login_username = document.querySelector("#TeacherUsername");
  let login_password = document.querySelector("#TeacherPassword");
  let login_btn = document.querySelector(".btn_green");

  setTimeout(() => {
    if (login_username && login_password && login_btn) {
      login_username.value = "xhani.iljard@gmail.com";
      login_password.value = "voiledofficialN@01";
      login_btn.click();
      console.log("clicked login");
    } else {
      console.log("Login elements not found!");
    }
  }, 5500);

}


function goStandby(){
  let status_area = document.querySelector(".area-status");
  let status_dropdown = document.querySelector("#status_select");
  let standby_btn = document.querySelector("#status_online a");

  if (status_area && status_area.innerText === "NOT STANDBY") {
    status_dropdown.style.display = "block";
    setTimeout(() => {
      standby_btn.click()
      console.log("clicked standby")
    }, 1000)
  } else {
    console.log("29: Status area not found or already standby!");
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}


// ================ Route =====================
function page() {
  const url = window.location.href;

  if (url.includes("/teacher/login")) return "login";
  if (url.includes("/teacher/home")) return "home";
  if (url.includes("/teacher/lesson-tutorial")) return "class";
  if (url.includes("chatgpt.com")) return "ai";
  if (url.includes("akool.com/apps/streaming-avatar/edit")) return "teacher";
  if (url.includes("speechtexter.com")) return "stt";

  return "unknown";
}


// =========== 