import { initializeApp } from 'firebase/app';
import { initializeAppCheck, ReCaptchaEnterpriseProvider } from 'firebase/app-check';
import { getAuth, GoogleAuthProvider, signInWithCredential, type User } from 'firebase/auth';
import { getFirestore } from 'firebase/firestore';

// Stagenator home project (state, ledger, playbook, directives)
export const app = initializeApp({
  projectId: 'operation-sunrise',
  appId: '1:243022700959:web:449c578aab87451c519daa',
  apiKey: 'AIzaSyB3Alkq6lYuNnU93u0WMCCmgkf1GOWymRg',
  authDomain: 'stagenator-mission.web.app',  // same-origin auth handler — no cross-origin iframe
});

// Auth + Firestore are App Check-enforced in this project (protecting the game);
// the dashboard attests via its own reCAPTCHA Enterprise key.
initializeAppCheck(app, {
  provider: new ReCaptchaEnterpriseProvider('6Le--ZYtAAAAAFkM6gTPzLwEuZt6FiPanr5eYVQ7'),
  isTokenAutoRefreshEnabled: true,
});

export const db = getFirestore(app);
export const auth = getAuth(app);

export const OWNER_EMAIL = 'indrekl@gmail.com';
export const GOOGLE_CLIENT_ID =
  '243022700959-sqtg0uj80dur8d0assi6vcb1r04fqtd5.apps.googleusercontent.com';

/**
 * Sign-in via Google Identity Services (no iframes/popup-handler machinery —
 * robust against third-party-cookie blocking). GIS hands us an ID token; we
 * exchange it with Firebase via signInWithCredential (pure REST).
 */
declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (cfg: object) => void;
          prompt: () => void;
          renderButton: (el: HTMLElement, cfg: object) => void;
        };
      };
    };
  }
}

let gisLoaded: Promise<void> | null = null;
export function loadGis(): Promise<void> {
  if (!gisLoaded) {
    gisLoaded = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://accounts.google.com/gsi/client';
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('GIS failed to load'));
      document.head.appendChild(s);
    });
  }
  return gisLoaded;
}

export async function gisSignIn(buttonEl: HTMLElement, onError: (msg: string) => void) {
  await loadGis();
  window.google!.accounts.id.initialize({
    client_id: GOOGLE_CLIENT_ID,
    callback: async (resp: { credential: string }) => {
      try {
        await signInWithCredential(auth, GoogleAuthProvider.credential(resp.credential));
      } catch (e) {
        onError(e instanceof Error ? e.message : String(e));
      }
    },
  });
  window.google!.accounts.id.renderButton(buttonEl, {
    theme: 'filled_black',
    size: 'large',
    shape: 'pill',
  });
}

export const isOwner = (user: User | null) => !!user && user.email === OWNER_EMAIL;
