window.addEventListener("DOMContentLoaded", () => {
  // --- Selectors ---
  const registerForm = document.querySelector("#register-form");
  const loginForm = document.querySelector("#login-form");
  const uploadForm = document.querySelector("#upload-form");
  const statusMessage = document.querySelector("#status-message");
  const fileInput = document.querySelector("#file-input");
  const check1 = document.querySelector("#check-option-1");
  const check2 = document.querySelector("#check-option-2");
  const toRegisterBtn = document.querySelector("#to-register-btn");
  const toLoginBtn = document.querySelector("#to-login-btn");

  // Define your Backend API URL here
  const API_BASE_URL = "https://api.memory-illumination.com"; [cite: 2]

  // --- Navigation Logic ---
  toRegisterBtn.addEventListener("click", () => {
    loginForm.style.display = "none";
    registerForm.style.display = "flex";
    statusMessage.textContent = ""; 
  });

  toLoginBtn.addEventListener("click", () => {
    registerForm.style.display = "none";
    loginForm.style.display = "flex";
    statusMessage.textContent = "";
  });

  check1.addEventListener('change', () => {
    if (check1.checked) check2.checked = false;
  });

  check2.addEventListener('change', () => {
    if (check2.checked) check1.checked = false;
  });

  // --- Register Form Logic ---
  registerForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    const username = document.querySelector("#reg-username-input").value;
    const password = document.querySelector("#reg-password-input").value;

    fetch(`${API_BASE_URL}/register`, { [cite: 2]
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
    .then(response => {
      if (!response.ok) throw new Error("Registration failed");
      return response.json();
    })
    .then(data => {
      registerForm.style.display = "none";
      loginForm.style.display = "flex";
      statusMessage.textContent = `Registration Successful! Please check your email.`;
    })
    .catch(error => {
      statusMessage.textContent = "Registration failed. Please try again.";
      console.error("Error:", error);
    });
  });

  // --- Login Form Logic ---
  loginForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    const username = document.querySelector("#login-username-input").value;
    const password = document.querySelector("#login-password-input").value;

    fetch(`${API_BASE_URL}/login`, { [cite: 2]
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
    .then(response => {
      if (!response.ok) throw new Error("Login failed");
      return response.json();
    })
    .then(data => {
      registerForm.style.display = "none";
      loginForm.style.display = "none";
      uploadForm.style.display = "flex";
      statusMessage.textContent = `Welcome ${username}, Please upload a file.`;
    })
    .catch(error => {
      statusMessage.textContent = "Login failed. Check credentials or email verification.";
      console.error("Error:", error);
    });
  });

  // --- Upload Form Logic ---
  uploadForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    const file = fileInput.files[0];

    if (file) {
      statusMessage.textContent = `Uploading ${file.name}...`;
      const formData = new FormData();
      formData.append("myFile", file); 

      const settings = {
        featureA: check1.checked,
        featureB: check2.checked,
      };
      formData.append("settings", JSON.stringify(settings));

      fetch(`${API_BASE_URL}/upload-endpoint`, { [cite: 2]
        method: "POST",
        body: formData,
      })
      .then(response => {
        if (!response.ok) throw new Error("Network response was not ok");
        return response.blob(); 
      })
      .then(imageBlob => {
        const imageUrl = URL.createObjectURL(imageBlob);
        const link = document.createElement('a');
        link.href = imageUrl;
        const baseName = file.name.replace(/\.[^/.]+$/, "");
        link.download = baseName + "_illuminated.png";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(imageUrl);
        statusMessage.textContent = "Processing complete! Download started.";
      })
      .catch(error => {
        console.error("Error:", error);
        statusMessage.textContent = "Upload failed. Check console for details.";
      });
    } else {
      statusMessage.textContent = "Please select a file first.";
    }
  });
});