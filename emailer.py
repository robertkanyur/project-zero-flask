from flask import Flask, request, render_template, abort
from dotenv import load_dotenv
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime
from redis import Redis
import rq
import time
import re
import stripe
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)
app.redis = Redis.from_url(os.getenv('REDIS_URL'))
app.tasks = rq.Queue('emailer-tasks', connection=app.redis)
CORS(app)

@app.route('/')
def status_page():
    return(render_template('main.html', content='The <strong>emailer</strong> microservice is running.'))

@app.route('/api/v1/form', methods=['GET','POST'])
def api_v1_form():
    if request.method == 'GET':
        return render_template('main.html', content='<strong>Access denied.</strong><br /><br />Your IP address has been logged and this incident has been reported to the authorities.')
    if not request.json and 'sourceForm' not in request.json:
        abort(400);
    if not request.headers.get('Authorization') == ('Bearer ' + os.getenv('AUTH')):
        abort(403);

    utc_now =  datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    filtered_data = _filter_form_data(request.json)

    html_content = _generate_email_message(filtered_data)
    sheets_job = app.tasks.enqueue(_add_row_to_sheet, 'Form Submissions', filtered_data, [], [], utc_now)
    email_job = app.tasks.enqueue(_send_email, html_content, utc_now, filtered_data)

    return '200'

@app.route('/api/v1/charge', methods=['GET','POST'])
def api_v1_charge():
    stripe.api_key = os.getenv("STRIPE")
    if request.method == 'GET':
        return render_template('main.html', content='<strong>Access denied.</strong><br /><br />Your IP address has been logged and this incident has been reported to the authorities.')

    stripe_customer = {}
    customer_list = stripe.Customer.list()
    utc_now =  datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if request.json['recurring'] == True:
        for i, customer in enumerate(customer_list["data"]):
            if customer["email"] == request.json['customerEmail']:
                stripe_customer = customer
                break

        if 'id' not in stripe_customer:
            stripe_customer = stripe.Customer.create(
                source=request.json['token']['id'],
                email=request.json['customerEmail'],
                name=request.json['customerName']
            )

        stripe_plan = stripe.Plan.create(
            amount=request.json['amount'],
            currency="usd",
            interval="month",
            product={
                "name": "Custom recurring donation"
            }
        )
        stripe_subscription = stripe.Subscription.create(
            customer=stripe_customer['id'],
            plan=stripe_plan['id']
        )

        stripe_charge = stripe.Charge.create(
            customer=stripe_customer['id'],
            amount=request.json['amount'],
            currency="usd",
            receipt_email=request.json['customerEmail'],
        )

        customer_info = stripe.Customer.retrieve(stripe_customer['id'])
        donation_sheet_job = app.tasks.enqueue(_add_row_to_sheet, 'Donations', request.json, stripe_charge, customer_info, utc_now)
        subscription_sheet_job = app.tasks.enqueue(_add_row_to_sheet, 'Subscriptions', request.json, stripe_subscription, customer_info, utc_now)

    if request.json['recurring'] == False:
        stripe_charge = stripe.Charge.create(
            amount=request.json['amount'],
            currency="usd",
            receipt_email=request.json['customerEmail'],
            source=request.json['token']['id']
        )

        donatino_sheet_job = app.tasks.enqueue(_add_row_to_sheet, 'Donations', request.json, stripe_charge, [], utc_now)

    return stripe_charge

# PRIVATE FUNCTIONS #

def _filter_form_data(data):
    filtered_data = {
        "source": "",
        "name": "",
        "email": "",
        "phone": "",
        "message": ""
    }

    subEmail = r"[^A-Za-z+@.0-9!#$%&'*+-/=?^_`{|}~]"
    subPhone = r"[^0-9]"

    if 'sourceForm' in data:
        filtered_data['source'] = data['sourceForm']
    if 'formName' in data:
        filtered_data['name'] = data['formName']
    if 'formEmail' in data:
        filtered_data['email'] = re.sub(subEmail, '', data['formEmail'])
    if 'formPhone' in data:
        filtered_data['phone'] = re.sub(subPhone, '', data['formPhone'])
        filtered_data['phone'] = filtered_data['phone'][:3] + '-' + filtered_data['phone'][3:6] + '-' + filtered_data['phone'][6:]
    if 'formMessage' in data:
        filtered_data['message'] = data['formMessage']

    return filtered_data

def _generate_email_message(data):
    if data['source'] == 'Contact':
        return(
            '<p>You should reach out to them as soon as possible. Here is their message and contact information:</p>' +
            '<p>Name: ' + data["name"] + '<br />Email: ' + data["email"] + '<br />Phone: ' + data["phone"] + '<br />Message: ' + data['message']
        )
    elif data['source'] == 'Team' or data['source'] == 'Serve':
        return(
            '<p>You should reach out to them as soon as possible. Here is their contact information:</p>' +
            '<p>Name: ' + data["name"] + '<br />Email: ' + data["email"] + '<br />Phone: ' + data["phone"]
        )
    else:
        return('Something went wrong.')

# REDIS FUNCTIONS #

def _add_row_to_sheet(sheet_name, data, stripe_data, stripe_customer, utc_now):
    try:
        sheet = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_name(
            './client_secret.json',
            ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        )).open_by_key("1kKqs94jLBiiatyjpx-t7kf4N2RpzjuiyPWOrqkQPI08").worksheet(sheet_name)
        number_of_rows = len(sheet.get_all_values()) + 1

        if sheet_name == 'Form Submissions':
            new_row = [utc_now,data['source'],data['name'],data['email'],data['phone'],data['message']]

            generated_range = ("A%s:F%s" %(number_of_rows, number_of_rows))
            cell_list = sheet.range(generated_range)
            for x, y in enumerate(new_row):
                cell_list[x].value = y
            sheet.update_cells(cell_list)

        if sheet_name == 'Donations':
            f_amount = '%.2f' % (stripe_data['amount'] / 100)

            if data['recurring'] == False:
                new_row = [utc_now,data['customerName'],data['customerEmail'],f_amount,stripe_data['status'],stripe_data['source']['brand'],stripe_data['source']['last4'],data['recurring'],stripe_data['receipt_url']]

            if data['recurring'] == True:
                new_row = [utc_now,stripe_customer['name'],stripe_customer['email'],f_amount,stripe_data['status'],stripe_data['source']['brand'],stripe_data['source']['last4'],data['recurring'],stripe_data['receipt_url']]

            generated_range = ("A%s:J%s" %(number_of_rows, number_of_rows))

        if sheet_name == 'Subscriptions':
            f_amount = '%.2f' % (stripe_data['plan']['amount'] / 100)
            f_link = 'http://dashboard.stripe.com/subscriptions/%s' %(stripe_data['id'])
            new_row = [utc_now,data['customerName'],data['customerEmail'],f_amount,true,f_link]
            generated_range = ("A%s:E%s" %(number_of_rows, number_of_rows))

        cell_list = sheet.range(generated_range)
        for x, y in enumerate(new_row):
            cell_list[x].value = y
        sheet.update_cells(cell_list)

        return '200'
    except Exception as e:
        return '500'

def _send_email(html_content, utc_now, data):
    message_source = data['source']
    subject = ("New form submission from the %s page (%s)" %(message_source, utc_now))
    message = Mail(
        from_email=os.getenv("FROM_EMAIL"),
        to_emails=os.getenv("TO_EMAIL"),
        subject=subject,
        html_content=html_content,
    )
    try:
        if os.getenv('SENDGRID_ENABLED') == 'True':
            sg = SendGridAPIClient(os.getenv('SENDGRID'))
            response = sg.send(message)
            return(str(response.status_code))
        else:
            return('200')
    except Exception as e:
        if os.getenv('SENDGRID_ENABLED') == 'True':
            return(str(e))
        else:
            abort(500)
