import { initializeApp } from 'firebase/app';
import { getAuth, GoogleAuthProvider, signInWithPopup, type User } from 'firebase/auth';
import { getFirestore } from 'firebase/firestore';

// Stagenator home project (state, ledger, playbook, directives)
export const app = initializeApp({
  projectId: 'operation-sunrise',
  appId: '1:243022700959:web:449c578aab87451c519daa',
  apiKey: 'AIzaSyB3Alkq6lYuNnU93u0WMCCmgkf1GOWymRg',
  authDomain: 'operation-sunrise.firebaseapp.com',
});

export const db = getFirestore(app);
export const auth = getAuth(app);

export const OWNER_EMAIL = 'indrekl@gmail.com';

export const signIn = () => signInWithPopup(auth, new GoogleAuthProvider());
export const isOwner = (user: User | null) => !!user && user.email === OWNER_EMAIL;
