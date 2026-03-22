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

  // --- Navigation Logic ---
  
  toRegisterBtn.addEventListener("click", () => {
    loginForm.style.display = "none";
    registerForm.style.display = "flex";
    statusMessage.textContent = ""; // Clear status
  });

  toLoginBtn.addEventListener("click", () => {
    registerForm.style.display = "none";
    loginForm.style.display = "flex";
    statusMessage.textContent = "";
  });

  check1.addEventListener('change', () => {
    if (check1.checked) {
      check2.checked = false;
    }
  });

  check2.addEventListener('change', () => {
    if (check2.checked) {
      check1.checked = false;
    }
  });

  // --- Register Form Logic ---
  registerForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    // UPDATED ID SELECTORS
    const username = document.querySelector("#reg-username-input").value;
    const password = document.querySelector("#reg-password-input").value;

    fetch("/register", {
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
      uploadForm.style.display = "none";
      statusMessage.textContent = `Registration Successful! Please log in`;
    })
    .catch(error => {
      statusMessage.textContent = "Registration failed. Please try again.";
      console.error("Error:", error);
    });
  });

  // --- Login Form Logic ---
  loginForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    // UPDATED ID SELECTORS
    const username = document.querySelector("#login-username-input").value;
    const password = document.querySelector("#login-password-input").value;

    fetch("/login", {
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
      statusMessage.textContent = "Login failed. Please try again.";
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
      
      // 2. Append the file
      formData.append("myFile", file); 

      // 3. Create a JSON object of the settings
      const settings = {
        featureA: check1.checked,
        featureB: check2.checked,
      };

      // 4. Append the settings as a JSON string
      // On the backend, you will parse this field (e.g., json.loads(request.form['settings']))
      formData.append("settings", JSON.stringify(settings));

      fetch("/upload-endpoint", {
        method: "POST",
        body: formData,
      })
      .then(response => {
        if (!response.ok) throw new Error("Network response was not ok");
        return response.blob(); 
      })
      .then(imageBlob => {
        // ... (Download logic remains the same) ...
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