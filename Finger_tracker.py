import os
import sys

# --- 1. Core Bundle Fix (Keeps the App from Crashing on Double-Click) ---
if getattr(sys, 'frozen', False):
    os.chdir(sys._MEIPASS)
else:
    if '__file__' in globals():
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

# --- 2. Imports ---
import cv2
import mediapipe as mp
import pyautogui
import numpy as np

# --- 3. System Guardrails & Sizing ---
pyautogui.FAILSAFE = True  
pyautogui.PAUSE = 0.001  
SCREEN_WIDTH, SCREEN_HEIGHT = pyautogui.size()

# AI Tracking Configurations
mp_hands = mp.solutions.hands
hands = mp_hands.Hands(
    static_image_mode=False,
    max_num_hands=1,
    model_complexity=1,       
    min_detection_confidence=0.85,
    min_tracking_confidence=0.85
)
mp_draw = mp.solutions.drawing_utils

def calculate_angle(p1, p2, p3):
    """Calculates the internal geometric angle of a finger to check for straightness."""
    a = np.array([p1.x, p1.y])
    b = np.array([p2.x, p2.y])
    c = np.array([p3.x, p3.y])
    
    ba = a - b
    bc = c - b
    
    cosine_angle = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc) + 1e-6)
    angle = np.degrees(np.arccos(np.clip(cosine_angle, -1.0, 1.0)))
    return angle

# --- 4. Dynamic Phone Stream Connector ---
def connect_to_phone():
    """Loops through hardware indices to locate your Continuity Phone Camera stream."""
    for index in [0, 1, 2]:
        print(f"[System] Attempting connection to Phone Camera on index {index}...")
        temp_cap = cv2.VideoCapture(index)
        success, test_frame = temp_cap.read()
        if success:
            print(f"[Success] Connected to camera index {index}!")
            return temp_cap
        temp_cap.release()
    
    print("[Warning] No external camera discovered. Falling back to default channel 0.")
    return cv2.VideoCapture(0)

cap = connect_to_phone()

# AI Smoothing & Click Lock States
smoothed_x, smoothed_y = 0, 0
SMOOTHING_ALPHA = 0.15  
click_executed = False  

print("\n🚀 AI Active! Viewpoint-independent tracking turned ON.")
print("Extend both fingers fully and touch their skeleton tips together to CLICK.")

try:
    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            continue

        frame = cv2.flip(frame, 1)
        h, w, c = frame.shape
        
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = hands.process(rgb_frame)

        if results.multi_hand_landmarks:
            for hand_landmarks in results.multi_hand_landmarks:
                mp_draw.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)

                landmarks = hand_landmarks.landmark
                
                # Extract full joint streams to calculate actual extension angles
                index_tip = landmarks[8]
                index_pip = landmarks[7]
                index_mcp = landmarks[5]
                
                middle_tip = landmarks[12]
                middle_pip = landmarks[11]
                middle_mcp = landmarks[9]

                # Convert to pixel coordinates for motion and distance checks
                ix, iy = int(index_pip.x * w), int(index_pip.y * h)      
                itx, ity = int(index_tip.x * w), int(index_tip.y * h)   
                mtx, mty = int(middle_tip.x * w), int(middle_tip.y * h) 

                # --- Steady Cursor Navigation ---
                pad_w = int(w * 0.15)
                pad_h = int(h * 0.15)
                
                target_x = np.interp(ix, (pad_w, w - pad_w), (0, SCREEN_WIDTH))
                target_y = np.interp(iy, (pad_h, h - pad_h), (0, SCREEN_HEIGHT))

                smoothed_x = (SMOOTHING_ALPHA * target_x) + ((1 - SMOOTHING_ALPHA) * smoothed_x)
                smoothed_y = (SMOOTHING_ALPHA * target_y) + ((1 - SMOOTHING_ALPHA) * smoothed_y)

                final_x = np.clip(smoothed_x, 0, SCREEN_WIDTH)
                final_y = np.clip(smoothed_y, 0, SCREEN_HEIGHT)
                pyautogui.moveTo(int(final_x), int(final_y))

                # --- Viewpoint-Independent Click Logic ---
                index_angle = calculate_angle(index_tip, index_pip, index_mcp)
                middle_angle = calculate_angle(middle_tip, middle_pip, middle_mcp)

                index_extended = index_angle > 155
                middle_extended = middle_angle > 155

                # Calculate the physical pixel distance between the outline tips
                pixel_distance = np.hypot(itx - mtx, ity - mty)

                # CRITERIA: Fingers MUST be fully uncurled/extended AND their outlines must touch
                if index_extended and middle_extended and pixel_distance < 25:
                    if not click_executed:
                        pyautogui.click()
                        cv2.circle(frame, (itx, ity), 15, (0, 255, 0), cv2.FILLED)
                        cv2.putText(frame, "CLICK!", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 3)
                        click_executed = True
                else:
                    if pixel_distance >= 25: 
                        click_executed = False  

        cv2.imshow("AI Assistive Phone View Mouse", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
finally:
    cap.release()
    cv2.destroyAllWindows()
    hands.close()