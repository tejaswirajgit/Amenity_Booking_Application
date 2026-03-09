# Netlify Deployment Guide

## Overview
This application consists of three deployable components:
1. **Admin Dashboard** - Next.js admin interface (`frontend/admin`)
2. **User Dashboard** - Next.js resident interface (`frontend/user`)
3. **Backend API** - FastAPI server (`backend`)

## Deploying to Netlify

### Admin Dashboard Deployment

1. **Connect Repository to Netlify**
   - Go to [Netlify](https://app.netlify.com/)
   - Click "Add new site" > "Import an existing project"
   - Connect to your GitHub repository: `https://github.com/tejaswirajgit/N1_AUDIO_TO_TEXT`

2. **Configure Build Settings**
   - The `frontend/admin/netlify.toml` file already contains the configuration
   - Netlify will auto-detect the settings
   - Base directory: `frontend/admin`
   - Build command: `npm install && npm run build`
   - Publish directory: `.next`

3. **Set Environment Variables in Netlify UI**
   
   Go to Site settings > Environment variables and add:
   
   ```
   NEXT_PUBLIC_SUPABASE_URL=https://vobavbcusbhsluovxwfw.supabase.co
   NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvYmF2YmN1c2Joc2x1b3Z4d2Z3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE4NDk3NjIsImV4cCI6MjA4NzQyNTc2Mn0.c6M33cE178lviDuPweyJwHYrAGs76Abhn1IsE8ug30U
   ADMIN_API_KEY=YTKqy-pm8NBJscBDZy9DbyuIlFhYbK7YBPYKRxpNHIw
   ADMIN_API_BASE_URL=https://your-backend-url.onrender.com
   ```
   
   **Important:** Update `ADMIN_API_BASE_URL` with your actual deployed backend URL (see Backend Deployment below)

4. **Deploy**
   - Click "Deploy site"
   - Netlify will build and deploy your admin dashboard

### User Dashboard Deployment

Follow the same process as Admin Dashboard:

1. Create a new site in Netlify (separate from admin)
2. Connect same repository
3. Build settings are in `frontend/user/netlify.toml`
4. Set environment variables:
   ```
   NEXT_PUBLIC_SUPABASE_URL=https://vobavbcusbhsluovxwfw.supabase.co
   NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZvYmF2YmN1c2Joc2x1b3Z4d2Z3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE4NDk3NjIsImV4cCI6MjA4NzQyNTc2Mn0.c6M33cE178lviDuPweyJwHYrAGs76Abhn1IsE8ug30U
   NEXT_PUBLIC_ADMIN_APP_URL=https://your-admin-app.netlify.app
   ```

## Backend Deployment (Render.com)

The backend should be deployed separately on Render.com:

1. Go to [Render Dashboard](https://dashboard.render.com/)
2. Create a new Web Service
3. Connect your GitHub repository
4. Configure:
   - **Root Directory:** `backend`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn api_server:app --host 0.0.0.0 --port $PORT`
   - **Environment:** Python 3.10

5. **Add Environment Variables:**
   ```
   SUPABASE_URL=https://vobavbcusbhsluovxwfw.supabase.co
   SUPABASE_SERVICE_KEY=<your-supabase-service-role-key>
   ADMIN_API_KEY=YTKqy-pm8NBJscBDZy9DbyuIlFhYbK7YBPYKRxpNHIw
   DATABASE_URL=<your-postgres-connection-string>
   ```

6. Once deployed, copy the backend URL (e.g., `https://your-app.onrender.com`)
7. Update the `ADMIN_API_BASE_URL` in your Admin Dashboard Netlify environment variables

## Post-Deployment Steps

1. **Update Admin Dashboard:**
   - Go to Admin Dashboard Netlify site settings
   - Update `ADMIN_API_BASE_URL` with actual backend URL
   - Redeploy

2. **Update User Dashboard:**
   - Go to User Dashboard Netlify site settings
   - Update `NEXT_PUBLIC_ADMIN_APP_URL` with actual admin dashboard URL
   - Redeploy

3. **Test the Applications:**
   - Admin Dashboard: Verify login, user management, bookings
   - User Dashboard: Verify resident login, booking features, profile
   - API: Test endpoints are responding

## Troubleshooting

### "Page not found" Error
- Verify the `base` directory is set correctly in `netlify.toml`
- Check build logs for any errors
- Ensure all environment variables are set
- Confirm Next.js plugin is installed

### Build Failures
- Check Node version (should be 20)
- Verify `npm install` completes successfully
- Look for TypeScript errors in build logs
- Ensure all dependencies are in `package.json`

### API Connection Issues
- Verify backend is deployed and running
- Check `ADMIN_API_BASE_URL` is correct
- Verify `ADMIN_API_KEY` matches between frontend and backend
- Check CORS settings in backend

### Authentication Issues
- Verify Supabase URLs and keys are correct
- Check Supabase project is active
- Verify user roles in Supabase dashboard

## Security Notes

⚠️ **Important Security Considerations:**

1. Never commit `.env.local` files to git
2. Rotate API keys periodically
3. Use Netlify environment variables for sensitive data
4. Keep Supabase service role key secret (backend only)
5. Enable CORS properly in backend for your frontend domains

## Support

For issues or questions:
- Check Netlify build logs
- Review Render backend logs
- Check Supabase dashboard for auth issues
- Verify all environment variables are set correctly
